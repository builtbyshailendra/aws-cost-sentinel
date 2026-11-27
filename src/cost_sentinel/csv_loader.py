"""Load AWS Cost Explorer CSV exports into a :class:`CostReport`.

Cost Explorer produces two layouts depending on where the export button was pressed:

* **services as rows** - the first column is the service name and each remaining
  column is a month (``Service,2026-05,2026-06,...,Service total``).
* **services as columns** - the first column is the month and each remaining
  column is a service (``Service,Amazon EC2($),Amazon RDS($),...,Total costs($)``).

Both are detected from the header and first column, "total" rows and columns are
dropped, the ``($)`` suffix is stripped from service names and dates are normalised
to ``YYYY-MM`` so that the rest of the code base only sees one shape.
"""

from __future__ import annotations

import csv
import logging
import re
from datetime import datetime
from pathlib import Path

from cost_sentinel.models import CostReport, CostSentinelError

logger = logging.getLogger(__name__)


class CsvFormatError(CostSentinelError):
    """The CSV could not be recognised as a Cost Explorer export."""


_CURRENCY_SUFFIX = re.compile(r"\s*\((\$|USD|EUR|GBP|INR)\)\s*$", re.IGNORECASE)
_TOTAL_LABEL = re.compile(r"^(service\s+)?total(\s+costs?)?$", re.IGNORECASE)
_DATE_FORMATS = (
    "%Y-%m",
    "%Y-%m-%d",
    "%Y-%m-%dT%H:%M:%SZ",
    "%Y-%m-%dT%H:%M:%S",
    "%b %Y",
    "%B %Y",
    "%m/%Y",
    "%m/%d/%Y",
)


def normalise_period(value: str) -> str | None:
    """Return ``YYYY-MM`` for any date format Cost Explorer emits, else None."""
    text = value.strip().strip('"')
    if not text:
        return None
    for fmt in _DATE_FORMATS:
        try:
            parsed = datetime.strptime(text, fmt)
        except ValueError:
            continue
        return parsed.strftime("%Y-%m")
    return None


def clean_service_name(value: str) -> str:
    """Strip the currency suffix and surrounding whitespace from a service label."""
    return _CURRENCY_SUFFIX.sub("", value.strip().strip('"')).strip()


def is_total_label(value: str) -> bool:
    """True for ``Total``, ``Service total``, ``Total costs`` and friends."""
    return bool(_TOTAL_LABEL.match(clean_service_name(value)))


def parse_amount(value: str, *, location: str) -> float:
    """Parse ``$1,234.56``, ``1234.56``, ``--`` or an empty cell into a float."""
    text = value.strip().strip('"').replace("$", "").replace(",", "")
    if text in {"", "--", "-", "n/a", "N/A"}:
        return 0.0
    try:
        return float(text)
    except ValueError as exc:
        raise CsvFormatError(f"cannot parse amount {value!r} at {location}") from exc


def _read_rows(path: Path) -> list[list[str]]:
    try:
        text = path.read_text(encoding="utf-8-sig")
    except FileNotFoundError as exc:
        raise CsvFormatError(f"CSV file not found: {path}") from exc
    rows = [row for row in csv.reader(text.splitlines()) if any(cell.strip() for cell in row)]
    if len(rows) < 2:
        raise CsvFormatError(f"{path} does not contain a header and at least one data row")
    return rows


def detect_orientation(header: list[str], body: list[list[str]]) -> str:
    """Return ``rows`` when services are rows, ``columns`` when services are columns."""
    header_cells = [cell for cell in header[1:] if cell.strip() and not is_total_label(cell)]
    header_dates = sum(1 for cell in header_cells if normalise_period(cell))
    if header_cells and header_dates == len(header_cells):
        return "rows"
    first_column = [row[0] for row in body if row and row[0].strip() and not is_total_label(row[0])]
    column_dates = sum(1 for cell in first_column if normalise_period(cell))
    if first_column and column_dates == len(first_column):
        return "columns"
    raise CsvFormatError(
        "could not detect the CSV layout: expected either month headers "
        "(services as rows) or month values in the first column (services as columns)"
    )


def _services_as_rows(header: list[str], body: list[list[str]], source: str) -> CostReport:
    period_columns: list[tuple[int, str]] = []
    for index, cell in enumerate(header[1:], start=1):
        period = normalise_period(cell)
        if period is not None:
            period_columns.append((index, period))
    periods = [period for _, period in period_columns]
    services: dict[str, list[float]] = {}
    for row_number, row in enumerate(body, start=2):
        label = row[0] if row else ""
        if not label.strip() or is_total_label(label):
            continue
        name = clean_service_name(label)
        values = [
            parse_amount(
                row[index] if index < len(row) else "",
                location=f"row {row_number}, column {index + 1}",
            )
            for index, _ in period_columns
        ]
        services[name] = _merge(services.get(name), values)
    return _build(periods, services, source)


def _services_as_columns(header: list[str], body: list[list[str]], source: str) -> CostReport:
    service_columns: list[tuple[int, str]] = [
        (index, clean_service_name(cell))
        for index, cell in enumerate(header[1:], start=1)
        if cell.strip() and not is_total_label(cell)
    ]
    periods: list[str] = []
    columns: dict[str, list[float]] = {name: [] for _, name in service_columns}
    for row_number, row in enumerate(body, start=2):
        label = row[0] if row else ""
        if not label.strip() or is_total_label(label):
            continue
        period = normalise_period(label)
        if period is None:
            raise CsvFormatError(f"row {row_number} does not start with a month: {label!r}")
        periods.append(period)
        for index, name in service_columns:
            columns[name].append(
                parse_amount(
                    row[index] if index < len(row) else "",
                    location=f"row {row_number}, column {index + 1}",
                )
            )
    return _build(periods, columns, source)


def _merge(existing: list[float] | None, values: list[float]) -> list[float]:
    """Sum duplicate service labels (Cost Explorer repeats them across linked accounts)."""
    if existing is None:
        return values
    return [left + right for left, right in zip(existing, values, strict=True)]


def _build(periods: list[str], services: dict[str, list[float]], source: str) -> CostReport:
    if not periods:
        raise CsvFormatError("no month columns or rows were found")
    if not services:
        raise CsvFormatError("no service rows or columns were found")
    order = sorted(range(len(periods)), key=lambda index: periods[index])
    sorted_periods = [periods[index] for index in order]
    sorted_services = {
        name: [values[index] for index in order] for name, values in services.items()
    }
    if len(set(sorted_periods)) != len(sorted_periods):
        raise CsvFormatError(f"duplicate month in export: {sorted_periods}")
    logger.info(
        "loaded %d services over %d months from %s", len(sorted_services), len(periods), source
    )
    return CostReport(periods=sorted_periods, services=sorted_services, source=source)


def load_cost_explorer_csv(path: str | Path) -> CostReport:
    """Parse a Cost Explorer CSV export in either orientation."""
    csv_path = Path(path)
    rows = _read_rows(csv_path)
    header = [cell.strip() for cell in rows[0]]
    body = rows[1:]
    orientation = detect_orientation(header, body)
    logger.debug("detected %s orientation for %s", orientation, csv_path)
    if orientation == "rows":
        return _services_as_rows(header, body, csv_path.as_posix())
    return _services_as_columns(header, body, csv_path.as_posix())
