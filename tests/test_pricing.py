"""Bundled price table lookups and extrapolation."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from cost_sentinel import pricing
from cost_sentinel.checks import IdleEc2Check, SavingsPlanCheck
from cost_sentinel.models import Instance, Inventory, ScanConfig


def test_exact_ec2_price_in_us_east_1_is_not_approximate() -> None:
    price = pricing.ec2_hourly("m5.large", "us-east-1")
    assert price is not None
    assert price.amount == pytest.approx(0.096)
    assert not price.approximate


def test_unknown_size_is_extrapolated_within_the_family() -> None:
    price = pricing.ec2_hourly("m5.8xlarge", "us-east-1")
    assert price is not None
    assert price.amount == pytest.approx(0.096 * 16)
    assert price.approximate
    assert "m5" in price.note


def test_unknown_family_and_malformed_types_return_none() -> None:
    assert pricing.ec2_hourly("zz9.large") is None
    assert pricing.ec2_hourly("garbage") is None


def test_region_factor_applies_and_marks_estimate_approximate() -> None:
    price = pricing.ec2_hourly("m5.large", "eu-central-1")
    assert price is not None
    assert price.amount == pytest.approx(0.096 * 1.15)
    assert price.approximate
    assert pricing.region_factor("xx-nowhere-1") == pricing.DEFAULT_REGION_FACTOR
    assert pricing.region_factor("global") == 1.0


def test_rds_multi_az_doubles_the_hourly_price() -> None:
    single = pricing.rds_hourly("db.m5.large", multi_az=False)
    multi = pricing.rds_hourly("db.m5.large", multi_az=True)
    assert single is not None
    assert multi is not None
    assert multi.amount == pytest.approx(2 * single.amount)


def test_gp3_equivalent_keeps_gp2_baseline_iops_for_large_volumes() -> None:
    assert pricing.gp2_baseline_iops(10) == 100
    assert pricing.gp2_baseline_iops(2000) == 6000
    assert pricing.gp2_baseline_iops(10_000) == 16000
    gp3 = pricing.gp3_equivalent_monthly(2000)
    assert gp3.amount == pytest.approx(2000 * 0.08 + 3000 * 0.005)


def test_ebs_monthly_includes_provisioned_performance() -> None:
    base = pricing.ebs_monthly("gp3", 100)
    boosted = pricing.ebs_monthly("gp3", 100, iops=4000, throughput_mbps=225)
    assert boosted.amount - base.amount == pytest.approx(1000 * 0.005 + 100 * 0.04)
    assert pricing.ebs_monthly("io1", 100, iops=1000).amount == pytest.approx(12.5 + 65)
    assert pricing.ebs_monthly("mystery", 10).approximate


def test_fixed_monthly_charges() -> None:
    assert pricing.elastic_ip_monthly().amount == pytest.approx(3.65)
    assert pricing.nat_gateway_monthly().amount == pytest.approx(32.85)
    assert pricing.elb_monthly("application").amount == pytest.approx(16.425)
    assert pricing.elb_monthly("classic").approximate


def test_windows_adds_the_per_vcpu_licence_premium() -> None:
    windows = pricing.ec2_hourly("m5.xlarge", "us-east-1", "Windows")
    assert windows is not None
    assert windows.amount == pytest.approx(0.376)  # AWS list price, Windows m5.xlarge
    burstable = pricing.ec2_hourly("t3.micro", "us-east-1", "Windows")
    assert burstable is not None
    assert burstable.amount == pytest.approx(0.0196)
    assert "Windows licence" in windows.note


@pytest.mark.parametrize(
    ("platform", "kind"),
    [
        ("Linux/UNIX", "linux"),
        (None, "linux"),
        ("Red Hat BYOL Linux", "linux"),
        ("Windows", "windows"),
        ("Windows with SQL Server Standard", "windows-sql"),
        ("Red Hat Enterprise Linux", "other-licensed"),
        ("SUSE Linux", "other-licensed"),
    ],
)
def test_platform_kind(platform: str | None, kind: str) -> None:
    assert pricing.platform_kind(platform) == kind


def test_unmodelled_licences_are_priced_as_linux_but_marked_approximate() -> None:
    linux = pricing.ec2_hourly("m5.large", "us-east-1")
    rhel = pricing.ec2_hourly("m5.large", "us-east-1", "Red Hat Enterprise Linux")
    assert linux is not None
    assert rhel is not None
    assert rhel.amount == pytest.approx(linux.amount)
    assert not linux.approximate
    assert rhel.approximate
    assert "licence not included" in rhel.note


def test_idle_windows_instance_is_priced_with_its_licence() -> None:
    now = datetime(2026, 11, 7, tzinfo=UTC)
    inventory = Inventory(region="us-east-1", collected_at=now)
    inventory.instances = [
        Instance(
            instance_id="i-win",
            instance_type="m5.xlarge",
            state="running",
            launch_time=now - timedelta(days=60),
            avg_cpu_percent=0.8,
            platform="Windows",
        ),
        Instance(
            instance_id="i-app",
            instance_type="m5.xlarge",
            state="running",
            launch_time=now - timedelta(days=60),
            avg_cpu_percent=40.0,
            platform="Windows",
        ),
    ]
    config = ScanConfig(reference_time=now)

    idle = IdleEc2Check().run(inventory, config)
    plan = SavingsPlanCheck().run(inventory, config)

    assert idle[0].estimated_monthly_savings == pytest.approx(0.376 * 730)
    assert idle[0].details["platform"] == "Windows"
    # The Savings Plan discount applies to the compute rate, not the licence premium.
    assert plan[0].monthly_cost == pytest.approx(0.376 * 730)
    assert plan[0].estimated_monthly_savings == pytest.approx(0.192 * 730 * 0.2)
    assert plan[0].details["licence_hourly"] == pytest.approx(0.184)
