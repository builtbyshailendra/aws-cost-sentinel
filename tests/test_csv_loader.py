"""Cost Explorer CSV parsing in both orientations."""

from __future__ import annotations

from pathlib import Path

import pytest

from cost_sentinel.csv_loader import (
    CsvFormatError,
    detect_orientation,
    load_cost_explorer_csv,
    normalise_period,
    parse_amount,
)
from tests.conftest import BY_MONTH_CSV, BY_SERVICE_CSV


def _write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "export.csv"
    path.write_text(text, encoding="utf-8")
    return path


def test_services_as_rows_example_loads() -> None:
    report = load_cost_explorer_csv(BY_SERVICE_CSV)
    assert report.periods == ["2026-05", "2026-06", "2026-07", "2026-08", "2026-09", "2026-10"]
    assert report.services["EC2-Instances"][0] == pytest.approx(4210.55)
    assert "Total costs" not in report.services
    assert len(report.services) == 11


def test_services_as_columns_example_loads() -> None:
    report = load_cost_explorer_csv(BY_MONTH_CSV)
    assert report.periods[0] == "2026-05"
    assert report.services["CloudWatch"][-1] == pytest.approx(168.92)
    assert "Service total" not in report.services


def test_both_orientations_produce_the_same_report() -> None:
    by_service = load_cost_explorer_csv(BY_SERVICE_CSV)
    by_month = load_cost_explorer_csv(BY_MONTH_CSV)
    assert by_service.periods == by_month.periods
    assert by_service.services.keys() == by_month.services.keys()
    for service, values in by_service.services.items():
        assert values == pytest.approx(by_month.services[service]), service
    assert by_service.grand_total() == pytest.approx(46534.79)


def test_orientation_detection() -> None:
    assert detect_orientation(["Service", "2026-01-01", "Total"], [["S3", "1"]]) == "rows"
    assert detect_orientation(["Service", "S3($)"], [["2026-01-01", "1"]]) == "columns"
    with pytest.raises(CsvFormatError, match="could not detect"):
        detect_orientation(["Service", "S3"], [["EC2", "1"]])


def test_byte_order_mark_currency_and_formatted_amounts(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        '﻿"Service","2026-01","2026-02"\n'
        '"Amazon EC2 (USD)","$1,234.50","--"\n'
        '"Amazon S3($)","",10\n',
    )
    report = load_cost_explorer_csv(path)
    assert report.services == {"Amazon EC2": [1234.5, 0.0], "Amazon S3": [0.0, 10.0]}


def test_duplicate_service_rows_are_summed(tmp_path: Path) -> None:
    path = _write(tmp_path, "Service,2026-01,2026-02\nS3,1,2\nS3,3,4\n")
    assert load_cost_explorer_csv(path).services["S3"] == [4.0, 6.0]


def test_unsorted_months_are_sorted(tmp_path: Path) -> None:
    path = _write(tmp_path, "Service,EC2($)\n2026-03-01,30\n2026-01-01,10\n2026-02-01,20\n")
    report = load_cost_explorer_csv(path)
    assert report.periods == ["2026-01", "2026-02", "2026-03"]
    assert report.services["EC2"] == [10.0, 20.0, 30.0]


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("2026-05", "2026-05"),
        ("2026-05-01", "2026-05"),
        ("2026-05-01T00:00:00Z", "2026-05"),
        ("May 2026", "2026-05"),
        ("05/01/2026", "2026-05"),
        ("EC2-Instances", None),
        ("", None),
    ],
)
def test_normalise_period(raw: str, expected: str | None) -> None:
    assert normalise_period(raw) == expected


def test_parse_amount_rejects_garbage() -> None:
    assert parse_amount(" 1,000.25 ", location="x") == pytest.approx(1000.25)
    with pytest.raises(CsvFormatError, match="row 3"):
        parse_amount("abc", location="row 3, column 2")


def test_missing_and_empty_files(tmp_path: Path) -> None:
    with pytest.raises(CsvFormatError, match="not found"):
        load_cost_explorer_csv(tmp_path / "nope.csv")
    with pytest.raises(CsvFormatError, match="header"):
        load_cost_explorer_csv(_write(tmp_path, "Service,2026-01\n"))


def test_duplicate_month_is_rejected(tmp_path: Path) -> None:
    path = _write(tmp_path, "Service,EC2\n2026-01-01,1\n2026-01-15,2\n")
    with pytest.raises(CsvFormatError, match="duplicate month"):
        load_cost_explorer_csv(path)
