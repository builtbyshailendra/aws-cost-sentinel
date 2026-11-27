"""Base class and helpers shared by every check."""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from datetime import datetime
from typing import ClassVar

from cost_sentinel.models import Finding, Inventory, ScanConfig

logger = logging.getLogger(__name__)


class BaseCheck(ABC):
    """A check inspects the inventory and returns zero or more findings.

    Checks are pure: they never call AWS, so they run in milliseconds and can be
    tested against hand-built inventories as well as moto-backed ones.
    """

    id: ClassVar[str]
    title: ClassVar[str]
    description: ClassVar[str]
    needs_cost_report: ClassVar[bool] = False
    needs_inventory: ClassVar[bool] = True
    """False for checks that can work from cost data alone (CSV mode)."""
    inputs: ClassVar[str] = "inventory"
    """Human-readable data source, shown by ``cost-sentinel checks``."""

    @abstractmethod
    def run(self, inventory: Inventory, config: ScanConfig) -> list[Finding]:
        """Return the findings for ``inventory`` under ``config``."""

    def __repr__(self) -> str:
        return f"<{type(self).__name__} {self.id}>"


def age_days(now: datetime, then: datetime) -> float:
    """Age in (fractional) days of a timestamp relative to ``now``."""
    delta = now - then
    return max(0.0, delta.total_seconds() / 86400.0)


def per_month(amount_over_window: float, lookback_days: int) -> float:
    """Scale a quantity measured over ``lookback_days`` to a 30-day month."""
    return amount_over_window * 30.0 / max(1, lookback_days)


def fmt_money(amount: float) -> str:
    """``$1,234.56`` formatting used in reasons and recommendations."""
    return f"${amount:,.2f}"
