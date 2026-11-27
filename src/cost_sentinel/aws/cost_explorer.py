"""Fetch monthly cost per service from the Cost Explorer API."""

from __future__ import annotations

import logging
from datetime import UTC, date, datetime
from typing import Any

from botocore.exceptions import BotoCoreError, ClientError

from cost_sentinel.models import CostReport, CostSentinelError

logger = logging.getLogger(__name__)


class CostExplorerError(CostSentinelError):
    """Cost Explorer could not be queried (not enabled, no permission, throttled)."""


def month_window(months: int, today: date | None = None) -> tuple[date, date]:
    """Start (inclusive) and end (exclusive) dates covering the last ``months`` full months."""
    reference = today or datetime.now(UTC).date()
    end = reference.replace(day=1)
    year, month = end.year, end.month - months
    while month <= 0:
        month += 12
        year -= 1
    return date(year, month, 1), end


def _normalise_period(start: str) -> str:
    return start[:7]


def fetch_cost_report(
    ce: Any,
    months: int = 6,
    *,
    today: date | None = None,
    metric: str = "UnblendedCost",
) -> CostReport:
    """Return monthly spend per service for the last ``months`` full months.

    Uses ``GetCostAndUsage`` grouped by ``SERVICE`` with monthly granularity and
    follows ``NextPageToken`` so that large accounts are fully covered.
    """
    start, end = month_window(months, today)
    request: dict[str, Any] = {
        "TimePeriod": {"Start": start.isoformat(), "End": end.isoformat()},
        "Granularity": "MONTHLY",
        "Metrics": [metric],
        "GroupBy": [{"Type": "DIMENSION", "Key": "SERVICE"}],
    }
    periods: list[str] = []
    services: dict[str, dict[str, float]] = {}
    currency = "USD"
    token: str | None = None
    while True:
        if token:
            request["NextPageToken"] = token
        try:
            response = ce.get_cost_and_usage(**request)
        except (ClientError, BotoCoreError) as exc:
            raise CostExplorerError(f"Cost Explorer GetCostAndUsage failed: {exc}") from exc
        for result in response.get("ResultsByTime", []):
            period = _normalise_period(result["TimePeriod"]["Start"])
            if period not in periods:
                periods.append(period)
            for group in result.get("Groups", []):
                name = group["Keys"][0]
                amount_info = group["Metrics"][metric]
                currency = amount_info.get("Unit", currency)
                amount = float(amount_info.get("Amount", "0") or 0.0)
                services.setdefault(name, {})[period] = (
                    services.get(name, {}).get(period, 0.0) + amount
                )
        token = response.get("NextPageToken")
        if not token:
            break
    periods.sort()
    aligned = {
        name: [by_period.get(period, 0.0) for period in periods]
        for name, by_period in services.items()
    }
    logger.info("cost explorer returned %d services over %d months", len(aligned), len(periods))
    return CostReport(
        periods=periods,
        services=aligned,
        currency=currency,
        source=f"cost-explorer:{start.isoformat()}..{end.isoformat()}",
    )
