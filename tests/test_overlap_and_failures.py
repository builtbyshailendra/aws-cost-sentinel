"""Savings must count each resource once, and a scan without data must fail, not pass."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any, ClassVar

import pytest
from botocore.exceptions import ClientError

from cost_sentinel import cli
from cost_sentinel.aws.collectors import CollectionError, collect_inventory, is_credential_error
from cost_sentinel.aws.session import AwsClients
from cost_sentinel.checks.base import BaseCheck
from cost_sentinel.csv_loader import load_cost_explorer_csv
from cost_sentinel.models import (
    Confidence,
    Finding,
    Instance,
    Inventory,
    ScanConfig,
    Volume,
)
from cost_sentinel.report import render
from cost_sentinel.scanner import IncompleteScanError, resolve_overlaps, run_scan, scan_live
from tests.conftest import BY_SERVICE_CSV, REGION, ROOT

NOW = datetime(2026, 11, 7, 12, 0, tzinfo=UTC)
LONG_AGO = NOW - timedelta(days=120)


def _config(**overrides: Any) -> ScanConfig:
    return ScanConfig(region=REGION, reference_time=NOW, **overrides)


def _instance(instance_id: str, instance_type: str, cpu: float, **kwargs: Any) -> Instance:
    return Instance(
        instance_id=instance_id,
        instance_type=instance_type,
        state="running",
        launch_time=LONG_AGO,
        avg_cpu_percent=cpu,
        **kwargs,
    )


# --- overlapping findings ----------------------------------------------------------


def test_unattached_gp2_volume_is_counted_once() -> None:
    inventory = Inventory(region=REGION, collected_at=NOW)
    inventory.volumes = [
        Volume(
            volume_id="vol-loose",
            size_gib=1000,
            volume_type="gp2",
            state="available",
            create_time=LONG_AGO,
        )
    ]

    result = run_scan(inventory, _config(), source="live")

    assert [f.check_id for f in result.findings] == ["unattached-ebs"]
    assert result.total_savings == pytest.approx(100.0)
    assert result.total_savings <= result.findings[0].monthly_cost


def test_savings_plan_leaves_out_instances_idle_ec2_wants_stopped() -> None:
    inventory = Inventory(region=REGION, collected_at=NOW)
    inventory.instances = [
        _instance("i-idle", "m5.xlarge", 1.3),
        _instance("i-busy", "c5.large", 46.0),
    ]

    result = run_scan(inventory, _config(), source="live")
    by_check = {f.check_id: f for f in result.findings}

    assert by_check["idle-ec2"].estimated_monthly_savings == pytest.approx(0.192 * 730)
    plan = by_check["savings-plan"]
    assert plan.details["instances"] == ["i-busy"]
    assert plan.details["excluded_idle_instances"] == ["i-idle"]
    assert plan.estimated_monthly_savings == pytest.approx(0.085 * 730 * 0.2)
    assert result.total_savings == pytest.approx(0.192 * 730 + 0.085 * 730 * 0.2)
    assert "1 idle instance(s) excluded" in plan.reason


def test_savings_plan_does_not_fall_back_to_cost_data_that_includes_idle_instances() -> None:
    inventory = Inventory(region=REGION, collected_at=NOW)
    inventory.instances = [_instance("i-idle", "m5.xlarge", 0.4)]
    inventory.cost_report = load_cost_explorer_csv(BY_SERVICE_CSV)

    result = run_scan(inventory, _config(checks=("idle-ec2", "savings-plan")), source="live")

    assert [f.check_id for f in result.findings] == ["idle-ec2"]


class _FixedCheck(BaseCheck):
    """Test double: every subclass reports one finding on the same volume."""

    description = "test double"
    needs_inventory = False
    savings: ClassVar[float] = 0.0
    confidence: ClassVar[Confidence] = Confidence.HIGH

    def run(self, inventory: Inventory, config: ScanConfig) -> list[Finding]:
        return [
            Finding(
                check_id=self.id,
                title=self.title,
                resource_id="vol-shared",
                resource_type="ec2:volume",
                region=inventory.region,
                monthly_cost=100.0,
                estimated_monthly_savings=self.savings,
                confidence=self.confidence,
                reason="test",
                recommendation="test",
            )
        ]


def _fixed(check_id: str, savings: float, confidence: Confidence) -> BaseCheck:
    attributes = {
        "id": check_id,
        "title": f"Check {check_id}",
        "savings": savings,
        "confidence": confidence,
    }
    return type(f"Fixed_{check_id}", (_FixedCheck,), attributes)()


def test_findings_on_the_same_resource_count_only_the_largest_saving() -> None:
    checks = [
        _fixed("delete-it", 100.0, Confidence.HIGH),
        _fixed("shrink-it", 20.0, Confidence.HIGH),
        _fixed("tier-it", 5.0, Confidence.LOW),
    ]
    inventory = Inventory(region=REGION, collected_at=NOW)

    result = run_scan(inventory, _config(), source="live", checks=checks)

    counted = {f.check_id: f.counted_in_total for f in result.findings}
    assert counted == {"delete-it": True, "shrink-it": False, "tier-it": False}
    assert result.total_savings == pytest.approx(100.0)
    assert result.gross_savings == pytest.approx(125.0)
    assert result.overlapping_savings == pytest.approx(25.0)
    loser = next(f for f in result.findings if f.check_id == "shrink-it")
    assert loser.details["overlaps_with"] == "delete-it"
    # The summary rows add up to the headline.
    assert sum(row.estimated_monthly_savings for row in result.summaries()) == pytest.approx(100.0)

    markdown = render(result, "markdown")
    assert "**Estimated monthly savings: $100.00**" in markdown
    assert "A further $25.00 is listed but not counted" in markdown
    assert "$20.00 (not counted; same resource as delete-it)" in markdown
    document = json.loads(render(result, "json"))
    assert document["summary"]["estimated_monthly_savings"] == 100.0
    assert document["summary"]["overlapping_savings_excluded"] == 25.0
    assert "not counted" in render(result, "html")


def test_resolve_overlaps_keeps_distinct_resources() -> None:
    base = Finding(
        check_id="a",
        title="A",
        resource_id="vol-1",
        resource_type="ec2:volume",
        region=REGION,
        monthly_cost=10.0,
        estimated_monthly_savings=10.0,
        confidence=Confidence.HIGH,
        reason="r",
        recommendation="r",
    )
    other_region = Finding(**{**base.__dict__, "check_id": "b", "region": "eu-west-1"})
    resolved = resolve_overlaps([base, other_region])
    assert all(f.counted_in_total for f in resolved)


def test_overlap_tie_prefers_the_more_confident_finding() -> None:
    checks = [_fixed("guess", 50.0, Confidence.LOW), _fixed("sure", 50.0, Confidence.HIGH)]
    inventory = Inventory(region=REGION, collected_at=NOW)
    result = run_scan(inventory, _config(), source="live", checks=checks)
    counted = [f.check_id for f in result.findings if f.counted_in_total]
    assert counted == ["sure"]


# --- failing closed ------------------------------------------------------------------


class _Raising:
    """A boto3-like client whose every paginator raises the given error."""

    def __init__(self, code: str, calls: list[str]) -> None:
        self._code = code
        self._calls = calls

    def get_paginator(self, name: str) -> _Raising:
        self._calls.append(name)
        return self

    def paginate(self, **_: Any) -> Any:
        raise ClientError({"Error": {"Code": self._code, "Message": "nope"}}, "Describe")

    def describe_addresses(self) -> Any:
        self._calls.append("describe_addresses")
        raise ClientError({"Error": {"Code": self._code, "Message": "nope"}}, "Describe")

    def list_buckets(self) -> Any:
        self._calls.append("list_buckets")
        raise ClientError({"Error": {"Code": self._code, "Message": "nope"}}, "ListBuckets")


def _broken_clients(aws: AwsClients, code: str, calls: list[str]) -> AwsClients:
    client = _Raising(code, calls)
    aws.ec2 = aws.elbv2 = aws.rds = aws.s3 = client
    return aws


def test_every_collector_failing_is_an_error_not_an_empty_report(aws: AwsClients) -> None:
    calls: list[str] = []
    _broken_clients(aws, "AccessDenied", calls)
    with pytest.raises(CollectionError, match="every service failed"):
        collect_inventory(aws, _config(), include_cost_report=False)
    assert len(calls) == 8  # each collector was tried before giving up


def test_invalid_credentials_abort_at_the_first_call(aws: AwsClients) -> None:
    calls: list[str] = []
    _broken_clients(aws, "InvalidClientTokenId", calls)
    with pytest.raises(CollectionError, match="credentials are missing or invalid"):
        collect_inventory(aws, _config(), include_cost_report=False)
    assert calls == ["describe_instances"]


def test_credential_error_classification() -> None:
    expired = ClientError({"Error": {"Code": "ExpiredToken", "Message": "x"}}, "Describe")
    denied = ClientError({"Error": {"Code": "AccessDenied", "Message": "x"}}, "Describe")
    assert is_credential_error(expired)
    assert not is_credential_error(denied)


def test_scan_without_any_credentials_exits_1_and_never_passes_the_gate(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    for name in (
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "AWS_SECURITY_TOKEN",
        "AWS_SESSION_TOKEN",
        "AWS_WEB_IDENTITY_TOKEN_FILE",
        "AWS_ROLE_ARN",
        "AWS_CONTAINER_CREDENTIALS_RELATIVE_URI",
        "AWS_CONTAINER_CREDENTIALS_FULL_URI",
    ):
        monkeypatch.delenv(name, raising=False)
    # Without this, botocore would ask the EC2 instance metadata service.
    monkeypatch.setenv("AWS_EC2_METADATA_DISABLED", "true")
    monkeypatch.setenv("BOTO_CONFIG", str(ROOT / "tests" / "missing-boto-config"))

    code = cli.main(["scan", "--region", REGION, "--fail-above", "0"])

    captured = capsys.readouterr()
    assert code == 1
    assert "credentials" in captured.err
    assert "No waste found" not in captured.out


class _DeniedRds:
    def get_paginator(self, _: str) -> _DeniedRds:
        return self

    def paginate(self, **_: Any) -> Any:
        raise ClientError({"Error": {"Code": "AccessDenied", "Message": "no"}}, "Describe")


def test_strict_scan_fails_when_one_service_is_skipped(aws: AwsClients) -> None:
    aws.rds = _DeniedRds()
    with pytest.raises(IncompleteScanError, match="rds:DescribeDBInstances"):
        scan_live(aws, _config(), include_cost_report=False, strict=True)
    result = scan_live(aws, _config(), include_cost_report=False)
    assert any(error.startswith("rds:") for error in result.inventory.errors)


def test_cli_warns_about_skipped_services_and_strict_turns_it_into_an_error(
    aws: AwsClients, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    aws.rds = _DeniedRds()
    monkeypatch.setattr(cli.AwsClients, "from_session", lambda *_args, **_kw: aws)
    args = ["scan", "--region", REGION, "--no-cost-explorer"]

    assert cli.main(args) == 0
    captured = capsys.readouterr()
    assert "1 service(s) could not be collected" in captured.err
    assert "rds:DescribeDBInstances" in captured.out  # listed under Skipped

    assert cli.main([*args, "--strict"]) == 1
    assert "--strict is set" in capsys.readouterr().err


def test_action_fails_on_every_exit_code_except_ok_and_gate() -> None:
    action = (ROOT / "action" / "action.yml").read_text(encoding="utf-8")
    assert '[ "$code" -ne 0 ] && [ "$code" -ne 3 ]' in action
    assert "--strict" in action


# --- CSV input and check selection -----------------------------------------------------


def test_unknown_check_id_is_an_argument_error(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as excinfo:
        cli.main(["report", "--csv", str(BY_SERVICE_CSV), "--checks", "bogus"])
    assert excinfo.value.code == 2
    assert "unknown check(s): bogus" in capsys.readouterr().err


def test_inventory_only_checks_on_csv_input_are_rejected(
    capsys: pytest.CaptureFixture[str],
) -> None:
    code = cli.main(["report", "--csv", str(BY_SERVICE_CSV), "--checks", "idle-ec2"])
    captured = capsys.readouterr()
    assert code == 2
    assert "idle-ec2 cannot run on a Cost Explorer export" in captured.err
    assert "No waste found" not in captured.out


def test_mixed_selection_on_csv_warns_and_lists_the_skipped_check(
    capsys: pytest.CaptureFixture[str],
) -> None:
    code = cli.main(["report", "--csv", str(BY_SERVICE_CSV), "--checks", "idle-ec2,spend-growth"])
    captured = capsys.readouterr()
    assert code == 0
    assert "warning: idle-ec2 cannot run" in captured.err
    assert "Checks not run: idle-ec2" in captured.out
    assert "spend-growth" in captured.out
