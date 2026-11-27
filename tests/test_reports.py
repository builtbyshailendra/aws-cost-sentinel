"""Markdown, HTML and JSON report writers."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from cost_sentinel.models import Confidence, Finding, Inventory, ScanConfig, ScanResult
from cost_sentinel.report import UnknownFormatError, render, write_report
from cost_sentinel.scanner import scan_csv
from tests.conftest import BY_SERVICE_CSV

FIXED_NOW = datetime(2026, 11, 7, 12, 0, tzinfo=UTC)


@pytest.fixture
def csv_result() -> ScanResult:
    return scan_csv(BY_SERVICE_CSV, ScanConfig(reference_time=FIXED_NOW))


@pytest.fixture
def hostile_result() -> ScanResult:
    """A live-mode result whose resource names try to break the markup."""
    finding = Finding(
        check_id="unattached-ebs",
        title="Unattached EBS volumes",
        resource_id="vol-<script>alert(1)</script>",
        resource_type="ec2:volume",
        region="eu-west-1",
        monthly_cost=10.7,
        estimated_monthly_savings=10.7,
        confidence=Confidence.HIGH,
        reason="100 GiB gp3 | named a|b has no attachment",
        recommendation="Delete it.",
    )
    inventory = Inventory(region="eu-west-1", collected_at=FIXED_NOW)
    inventory.errors.append("rds:DescribeDBInstances: AccessDenied")
    return ScanResult(
        findings=[finding],
        inventory=inventory,
        config=ScanConfig(region="eu-west-1", reference_time=FIXED_NOW),
        generated_at=FIXED_NOW,
        source="live",
        source_detail="profile=default region=eu-west-1",
        check_titles={"unattached-ebs": "Unattached EBS volumes"},
    )


def test_markdown_report_has_summary_findings_and_spend(csv_result: ScanResult) -> None:
    text = render(csv_result, "markdown")
    assert text.startswith("# AWS Cost Sentinel report")
    assert "**Estimated monthly savings: $1,302.11**" in text
    assert "| Savings Plan opportunity (`savings-plan`) | 1 | $851.02 |" in text
    assert "## Spend by service (2026-05 to 2026-10)" in text
    assert "| EC2-Other | $1,048.65 | $668.38 | +57% |" in text
    assert "## Resources scanned" not in text  # CSV mode has no inventory


def test_markdown_escapes_table_pipes_and_lists_skipped_services(
    hostile_result: ScanResult,
) -> None:
    text = render(hostile_result, "md")
    assert "named a\\|b" in text
    assert "## Skipped" in text
    assert "## Resources scanned" in text


def test_html_report_is_self_contained_and_escaped(
    csv_result: ScanResult, hostile_result: ScanResult
) -> None:
    page = render(csv_result, "html")
    assert page.startswith("<!DOCTYPE html>")
    assert "<style>" in page
    assert "<script" not in page
    assert "http://" not in page
    assert "https://" not in page
    assert "$1,302.11" in page
    hostile = render(hostile_result, "html")
    assert "<script>alert(1)</script>" not in hostile
    assert "&lt;script&gt;" in hostile
    assert 'class="badge high"' in hostile


def test_json_report_is_machine_readable(csv_result: ScanResult) -> None:
    document = json.loads(render(csv_result, "json"))
    assert document["schema_version"] == 1
    assert document["source"] == "csv"
    assert document["summary"]["finding_count"] == 3
    assert document["summary"]["estimated_monthly_savings"] == pytest.approx(1302.11, abs=0.01)
    assert document["cost_report"]["periods"][-1] == "2026-10"
    assert document["pricing"]["table_version"]
    assert {f["check_id"] for f in document["findings"]} == {"savings-plan", "spend-growth"}


def test_empty_result_renders_in_every_format() -> None:
    result = ScanResult(
        findings=[],
        inventory=Inventory(region="us-east-1", collected_at=FIXED_NOW),
        config=ScanConfig(reference_time=FIXED_NOW),
        generated_at=FIXED_NOW,
        source="live",
    )
    assert "No waste found" in render(result, "markdown")
    assert "No waste found" in render(result, "html")
    assert json.loads(render(result, "json"))["findings"] == []


def test_unknown_format_and_write_report(csv_result: ScanResult, tmp_path: Path) -> None:
    with pytest.raises(UnknownFormatError, match="pdf"):
        render(csv_result, "pdf")
    path = write_report(csv_result, "html", tmp_path / "nested" / "report.html")
    assert path.read_text(encoding="utf-8").startswith("<!DOCTYPE html>")


def test_notes_depend_on_the_source(csv_result: ScanResult, hostile_result: ScanResult) -> None:
    csv_text = render(csv_result, "markdown")
    live_text = render(hostile_result, "markdown")
    assert "only the savings-plan and spend-growth checks run" in csv_text
    assert "CloudWatch lookback" not in csv_text
    assert "CloudWatch lookback: 14 days" in live_text


def test_written_reports_use_unix_line_endings(csv_result: ScanResult, tmp_path: Path) -> None:
    path = write_report(csv_result, "markdown", tmp_path / "report.md")
    assert b"\r\n" not in path.read_bytes()
