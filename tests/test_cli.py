"""The ``cost-sentinel`` command line, run in-process."""

from __future__ import annotations

import importlib.util
import json
from datetime import UTC, datetime
from pathlib import Path

import boto3
import pytest
from moto import mock_aws

from cost_sentinel import __version__, cli, github
from cost_sentinel.github import HttpResponse
from tests.conftest import BY_MONTH_CSV, BY_SERVICE_CSV, REGION, ROOT

DEMO_SCRIPT = ROOT / "scripts" / "demo_moto_scan.py"


def test_version_flag(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as excinfo:
        cli.main(["--version"])
    assert excinfo.value.code == 0
    assert __version__ in capsys.readouterr().out


def test_report_prints_markdown_for_both_csv_orientations(
    capsys: pytest.CaptureFixture[str],
) -> None:
    outputs = []
    for path in (BY_SERVICE_CSV, BY_MONTH_CSV):
        assert cli.main(["report", "--csv", str(path)]) == 0
        outputs.append(capsys.readouterr().out)
    assert "Estimated monthly savings: $1,302.11" in outputs[0]
    # Only the source path differs between the two reports.
    assert outputs[0].replace(BY_SERVICE_CSV.as_posix(), "X") == outputs[1].replace(
        BY_MONTH_CSV.as_posix(), "X"
    )


def test_report_writes_json_to_a_file(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    target = tmp_path / "out" / "report.json"
    code = cli.main(
        ["report", "--csv", str(BY_SERVICE_CSV), "--format", "json", "--output", str(target)]
    )
    assert code == 0
    document = json.loads(target.read_text(encoding="utf-8"))
    assert document["summary"]["finding_count"] == 3
    assert "wrote json report" in capsys.readouterr().err


def test_fail_above_returns_exit_code_3(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["report", "--csv", str(BY_SERVICE_CSV), "--fail-above", "1000"]) == 3
    assert "exceed --fail-above" in capsys.readouterr().err
    assert cli.main(["report", "--csv", str(BY_SERVICE_CSV), "--fail-above", "5000"]) == 0


def test_format_can_come_from_the_environment(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("COST_SENTINEL_FORMAT", "json")
    assert cli.main(["report", "--csv", str(BY_SERVICE_CSV)]) == 0
    assert json.loads(capsys.readouterr().out)["source"] == "csv"


def test_errors_are_reported_with_exit_code_1(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    assert cli.main(["report", "--csv", str(tmp_path / "missing.csv")]) == 1
    assert "CSV file not found" in capsys.readouterr().err
    monkeypatch.setenv("COST_SENTINEL_LOOKBACK_DAYS", "two weeks")
    assert cli.main(["report", "--csv", str(BY_SERVICE_CSV)]) == 1
    assert "must be an integer" in capsys.readouterr().err


def test_checks_command_lists_every_check(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["checks"]) == 0
    out = capsys.readouterr().out
    for check_id in ("idle-ec2", "unattached-ebs", "gp2-to-gp3", "s3-lifecycle", "savings-plan"):
        assert check_id in out


def test_scan_against_moto(capsys: pytest.CaptureFixture[str]) -> None:
    with mock_aws():
        ec2 = boto3.client("ec2", region_name=REGION)
        ec2.create_volume(AvailabilityZone=f"{REGION}a", Size=100, VolumeType="gp2")
        ec2.allocate_address(Domain="vpc")
        code = cli.main(["scan", "--region", REGION, "--no-cost-explorer", "--format", "json"])
    assert code == 0
    document = json.loads(capsys.readouterr().out)
    assert document["source"] == "live"
    assert document["region"] == REGION
    checks = {finding["check_id"] for finding in document["findings"]}
    # The unattached gp2 volume is reported once, as a deletion, not also as a gp2 to
    # gp3 conversion; spend-growth reports that it had no Cost Explorer data.
    assert checks == {"unattached-ebs", "unused-eip"}
    assert document["summary"]["estimated_monthly_savings"] == pytest.approx(10.0 + 3.65)
    assert "spend-growth" in document["skipped_checks"]


def test_scan_with_unknown_profile_fails_cleanly(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["scan", "--profile", "does-not-exist", "--region", REGION]) == 1
    assert "does-not-exist" in capsys.readouterr().err


def test_comment_command_posts_the_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    sent: list[tuple[str, str]] = []

    def fake_transport(
        method: str, url: str, headers: dict[str, str], body: bytes | None
    ) -> HttpResponse:
        sent.append((method, url))
        if method == "GET":
            return HttpResponse(200, b"[]", {})
        return HttpResponse(201, b'{"html_url": "https://github.example/c/9"}', {})

    monkeypatch.setattr(github, "urllib_transport", fake_transport)
    report = tmp_path / "report.md"
    report.write_text("# AWS Cost Sentinel report\n", encoding="utf-8")
    event = tmp_path / "event.json"
    event.write_text(json.dumps({"pull_request": {"number": 5}}), encoding="utf-8")
    monkeypatch.setenv("GITHUB_EVENT_PATH", str(event))
    monkeypatch.setenv("GITHUB_TOKEN", "test-token")

    assert cli.main(["comment", "--file", str(report), "--repo", "quillfern/infra"]) == 0
    assert "created comment https://github.example/c/9" in capsys.readouterr().out
    assert sent[-1] == ("POST", "https://api.github.com/repos/quillfern/infra/issues/5/comments")


def test_comment_without_pull_request_fails(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    report = tmp_path / "report.md"
    report.write_text("x", encoding="utf-8")
    assert cli.main(["comment", "--file", str(report), "--repo", "quillfern/infra"]) == 1
    assert "no pull request number" in capsys.readouterr().err


def test_demo_script_scans_a_seeded_moto_account(capsys: pytest.CaptureFixture[str]) -> None:
    spec = importlib.util.spec_from_file_location("demo_moto_scan", DEMO_SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.main(["--format", "json"]) == 0
    document = json.loads(capsys.readouterr().out)
    found = {finding["check_id"] for finding in document["findings"]}
    summary = document["summary"]
    # The headline equals the per-check rows, and the idle instance is not also in the
    # Savings Plan baseline.
    assert summary["estimated_monthly_savings"] == pytest.approx(
        sum(row["estimated_monthly_savings"] for row in summary["by_check"]), abs=0.01
    )
    idle = next(f for f in document["findings"] if f["check_id"] == "idle-ec2")
    plan = next(f for f in document["findings"] if f["check_id"] == "savings-plan")
    assert idle["resource_id"] not in plan["details"]["instances"]
    assert plan["details"]["excluded_idle_instances"] == [idle["resource_id"]]
    # The demo back-dates resources instead of moving the clock, so the report is dated today.
    assert document["generated_at"][:10] == datetime.now(UTC).date().isoformat()
    expected = {
        "idle-ec2",
        "unattached-ebs",
        "old-snapshots",
        "unused-eip",
        "idle-load-balancer",
        "nat-gateway",
        "gp2-to-gp3",
        "idle-rds",
        "s3-lifecycle",
        "savings-plan",
    }
    assert found == expected
