"""Cost Explorer API collection, cost-report checks and the scanner pipeline."""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any

import boto3
import pytest
from botocore.exceptions import ClientError
from botocore.stub import Stubber

from cost_sentinel.aws.collectors import collect_inventory
from cost_sentinel.aws.cost_explorer import CostExplorerError, fetch_cost_report, month_window
from cost_sentinel.aws.metrics import choose_period
from cost_sentinel.aws.session import AwsClients
from cost_sentinel.checks import SavingsPlanCheck, SpendGrowthCheck, UnknownCheckError, get_checks
from cost_sentinel.csv_loader import load_cost_explorer_csv
from cost_sentinel.models import Confidence, CostReport, Inventory, ScanConfig
from cost_sentinel.scanner import run_scan, scan_csv
from tests.conftest import BY_MONTH_CSV, BY_SERVICE_CSV

FIXED_NOW = datetime(2026, 11, 7, 12, 0, tzinfo=UTC)


def _group(service: str, amount: str) -> dict[str, Any]:
    return {"Keys": [service], "Metrics": {"UnblendedCost": {"Amount": amount, "Unit": "USD"}}}


def test_month_window_covers_full_previous_months() -> None:
    assert month_window(6, date(2026, 11, 7)) == (date(2026, 5, 1), date(2026, 11, 1))
    assert month_window(3, date(2026, 2, 15)) == (date(2025, 11, 1), date(2026, 2, 1))


def test_cloudwatch_period_stays_under_the_datapoint_limit() -> None:
    assert choose_period(1) == 300
    assert choose_period(14) == 3600
    assert choose_period(90) == 21600


def test_fetch_cost_report_follows_pagination_and_aligns_months() -> None:
    ce = boto3.client("ce", region_name="us-east-1")
    expected_base = {
        "TimePeriod": {"Start": "2026-09-01", "End": "2026-11-01"},
        "Granularity": "MONTHLY",
        "Metrics": ["UnblendedCost"],
        "GroupBy": [{"Type": "DIMENSION", "Key": "SERVICE"}],
    }
    with Stubber(ce) as stubber:
        stubber.add_response(
            "get_cost_and_usage",
            {
                "ResultsByTime": [
                    {
                        "TimePeriod": {"Start": "2026-09-01", "End": "2026-10-01"},
                        "Groups": [_group("Amazon Elastic Compute Cloud - Compute", "100.5")],
                    }
                ],
                "NextPageToken": "page-2",
            },
            expected_base,
        )
        stubber.add_response(
            "get_cost_and_usage",
            {
                "ResultsByTime": [
                    {
                        "TimePeriod": {"Start": "2026-10-01", "End": "2026-11-01"},
                        "Groups": [
                            _group("Amazon Elastic Compute Cloud - Compute", "120"),
                            _group("Amazon Simple Storage Service", "7.25"),
                        ],
                    }
                ]
            },
            {**expected_base, "NextPageToken": "page-2"},
        )
        report = fetch_cost_report(ce, months=2, today=date(2026, 11, 7))

    assert report.periods == ["2026-09", "2026-10"]
    assert report.services["Amazon Elastic Compute Cloud - Compute"] == [100.5, 120.0]
    assert report.services["Amazon Simple Storage Service"] == [0.0, 7.25]
    assert report.currency == "USD"


def test_fetch_cost_report_wraps_access_errors() -> None:
    ce = boto3.client("ce", region_name="us-east-1")
    with Stubber(ce) as stubber:
        stubber.add_client_error("get_cost_and_usage", "AccessDeniedException", "not allowed")
        with pytest.raises(CostExplorerError, match="AccessDenied"):
            fetch_cost_report(ce, months=1, today=date(2026, 11, 7))


class _DeniedPaginator:
    def paginate(self, **_: Any) -> Any:
        raise ClientError(
            {"Error": {"Code": "AccessDenied", "Message": "no rds"}}, "DescribeDBInstances"
        )


class _DeniedRds:
    def get_paginator(self, _: str) -> _DeniedPaginator:
        return _DeniedPaginator()


def test_collector_errors_degrade_the_scan_instead_of_aborting(aws: AwsClients) -> None:
    aws.rds = _DeniedRds()
    ce_stub = Stubber(aws.ce)
    ce_stub.add_client_error("get_cost_and_usage", "DataUnavailableException", "not enabled")
    config = ScanConfig(region="us-east-1", reference_time=FIXED_NOW)
    with ce_stub:
        inventory = collect_inventory(aws, config)
    assert any(error.startswith("rds:DescribeDBInstances") for error in inventory.errors)
    assert any(error.startswith("ce:GetCostAndUsage") for error in inventory.errors)
    assert inventory.db_instances == []
    assert inventory.cost_report is None


def test_spend_growth_flags_services_above_their_trailing_average() -> None:
    report = load_cost_explorer_csv(BY_SERVICE_CSV)
    inventory = Inventory.from_cost_report(report)
    findings = {f.resource_id: f for f in SpendGrowthCheck().run(inventory, ScanConfig())}
    assert set(findings) == {"EC2-Other", "CloudWatch"}
    assert findings["EC2-Other"].estimated_monthly_savings == pytest.approx(1048.65 - 668.38)
    assert findings["CloudWatch"].details["growth_ratio"] == pytest.approx(0.722, abs=1e-3)


def test_spend_growth_needs_three_months_and_ignores_tiny_services() -> None:
    short = CostReport(periods=["2026-09", "2026-10"], services={"EC2": [100.0, 500.0]})
    tiny = CostReport(periods=["2026-08", "2026-09", "2026-10"], services={"KMS": [1.0, 1.0, 9.0]})
    check = SpendGrowthCheck()
    assert check.run(Inventory.from_cost_report(short), ScanConfig()) == []
    assert check.run(Inventory.from_cost_report(tiny), ScanConfig()) == []


def test_savings_plan_falls_back_to_cost_data() -> None:
    report = load_cost_explorer_csv(BY_MONTH_CSV)
    findings = SavingsPlanCheck().run(Inventory.from_cost_report(report), ScanConfig())
    assert len(findings) == 1
    assert findings[0].resource_id == "EC2-Instances"
    assert findings[0].monthly_cost == pytest.approx(4255.10)
    assert findings[0].estimated_monthly_savings == pytest.approx(4255.10 * 0.2)
    assert findings[0].confidence is Confidence.MEDIUM


def test_cost_report_rejects_misaligned_series() -> None:
    with pytest.raises(ValueError, match="2 values"):
        CostReport(periods=["2026-10"], services={"EC2": [1.0, 2.0]})


def test_scan_csv_orders_findings_and_applies_min_savings() -> None:
    config = ScanConfig(reference_time=FIXED_NOW)
    result = scan_csv(BY_SERVICE_CSV, config)
    savings = [f.estimated_monthly_savings for f in result.findings]
    assert savings == sorted(savings, reverse=True)
    assert result.findings[0].check_id == "savings-plan"
    assert result.total_savings == pytest.approx(851.02 + 380.27 + 70.82, abs=0.01)

    filtered = scan_csv(BY_SERVICE_CSV, ScanConfig(reference_time=FIXED_NOW, min_savings=100))
    assert {f.resource_id for f in filtered.findings} == {"EC2-Instances", "EC2-Other"}


def test_check_selection_and_unknown_ids() -> None:
    assert [check.id for check in get_checks(["idle-ec2", "nat-gateway"])] == [
        "idle-ec2",
        "nat-gateway",
    ]
    with pytest.raises(UnknownCheckError, match="no-such-check"):
        get_checks(["no-such-check"])
    result = run_scan(
        Inventory.from_cost_report(load_cost_explorer_csv(BY_SERVICE_CSV)),
        ScanConfig(checks=("spend-growth",), reference_time=FIXED_NOW),
        source="csv",
    )
    assert {f.check_id for f in result.findings} == {"spend-growth"}


def test_scan_config_validates_ratios() -> None:
    with pytest.raises(ValueError, match="savings_plan_discount"):
        ScanConfig(savings_plan_discount=1.5)
    with pytest.raises(ValueError, match="lookback_days"):
        ScanConfig(lookback_days=0)
