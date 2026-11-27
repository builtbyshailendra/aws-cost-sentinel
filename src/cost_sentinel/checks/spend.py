"""Cost-report checks that need no resource inventory."""

from __future__ import annotations

import logging

from cost_sentinel.checks.base import BaseCheck, fmt_money
from cost_sentinel.models import Confidence, Finding, Inventory, ScanConfig

logger = logging.getLogger(__name__)

MIN_BASELINE = 10.0
"""Services averaging less than this per month are too small to flag for growth."""
MIN_MONTHS = 3


class SpendGrowthCheck(BaseCheck):
    """Services whose latest month is well above their trailing average."""

    id = "spend-growth"
    title = "Services with unexplained spend growth"
    description = (
        "Compares the latest month of each service with the average of the earlier months "
        "in the export. Growth above the threshold is reported with the difference as a "
        "low-confidence saving: it is money to investigate, not money that is certainly waste."
    )
    needs_cost_report = True
    needs_inventory = False
    inputs = "cost data"

    def run(self, inventory: Inventory, config: ScanConfig) -> list[Finding]:
        report = inventory.cost_report
        if report is None or report.months < MIN_MONTHS:
            logger.debug("spend-growth needs at least %d months of cost data", MIN_MONTHS)
            return []
        findings: list[Finding] = []
        for service, values in report.services.items():
            average = report.trailing_average(service)
            if average is None or average < MIN_BASELINE:
                continue
            latest = values[-1]
            growth = (latest - average) / average
            if growth < config.spend_growth_threshold:
                continue
            findings.append(
                Finding(
                    check_id=self.id,
                    title=self.title,
                    resource_id=service,
                    resource_type="cost-explorer:service",
                    region=inventory.region,
                    monthly_cost=latest,
                    estimated_monthly_savings=latest - average,
                    confidence=Confidence.LOW,
                    reason=(
                        f"{report.periods[-1]} cost {fmt_money(latest)} against a trailing "
                        f"average of {fmt_money(average)} (+{growth * 100:.0f}%)"
                    ),
                    recommendation=(
                        "Open Cost Explorer for this service grouped by usage type and linked "
                        "account to confirm the increase is intended; set an AWS Budgets alert "
                        "at the trailing average."
                    ),
                    details={
                        "latest_period": report.periods[-1],
                        "latest": round(latest, 2),
                        "trailing_average": round(average, 2),
                        "growth_ratio": round(growth, 3),
                        "months": report.months,
                    },
                )
            )
        return findings
