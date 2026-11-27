"""Static checks on the GitHub Action and the workflows that use it."""

from __future__ import annotations

from typing import Any

import yaml

from tests.conftest import ROOT


def _load(path: str) -> dict[Any, Any]:
    return yaml.safe_load((ROOT / path).read_text(encoding="utf-8"))


def test_action_is_a_composite_that_scans_and_comments() -> None:
    action = _load("action/action.yml")
    assert action["runs"]["using"] == "composite"
    scripts = "\n".join(step.get("run", "") for step in action["runs"]["steps"])
    assert "cost-sentinel scan" in scripts
    assert "cost-sentinel report --csv" in scripts
    assert "cost-sentinel comment" in scripts
    assert set(action["inputs"]) >= {"mode", "csv", "aws-region", "fail-above", "github-token"}
    assert set(action["outputs"]) == {"report-path", "exit-code"}


def test_workflows_reference_existing_files() -> None:
    ci = _load(".github/workflows/ci.yml")
    # PyYAML parses the bare key "on" as boolean True.
    assert ci[True]["push"]["branches"] == ["main"]
    matrix = ci["jobs"]["test"]["strategy"]["matrix"]
    assert set(matrix["os"]) == {"ubuntu-latest", "windows-latest"}
    report = _load(".github/workflows/cost-report.yml")
    step = next(s for s in report["jobs"]["cost-report"]["steps"] if s.get("uses") == "./action")
    assert (ROOT / step["with"]["csv"]).is_file()
