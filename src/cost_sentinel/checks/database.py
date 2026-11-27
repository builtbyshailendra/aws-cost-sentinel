"""RDS checks: idle database instances."""

from __future__ import annotations

import logging

from cost_sentinel import pricing
from cost_sentinel.checks.base import BaseCheck, age_days
from cost_sentinel.models import Confidence, Finding, Inventory, ScanConfig

logger = logging.getLogger(__name__)


class IdleRdsCheck(BaseCheck):
    """Available RDS instances with (almost) no connections and low CPU."""

    id = "idle-rds"
    title = "Idle RDS instances"
    description = (
        "Database instances whose average connection count stayed under the threshold and "
        "whose CPU stayed low over the lookback window. Instances without CloudWatch data "
        "are skipped. Savings cover the instance hours; storage is kept by the final snapshot."
    )

    def run(self, inventory: Inventory, config: ScanConfig) -> list[Finding]:
        findings: list[Finding] = []
        for database in inventory.db_instances:
            if database.status != "available":
                continue
            if database.avg_connections is None:
                logger.debug("%s has no connection datapoints; skipping", database.db_instance_id)
                continue
            if database.avg_connections >= config.rds_connections_threshold:
                continue
            if (
                database.avg_cpu_percent is not None
                and database.avg_cpu_percent >= config.rds_cpu_threshold
            ):
                logger.debug(
                    "%s has few connections but %.1f%% CPU; not idle",
                    database.db_instance_id,
                    database.avg_cpu_percent,
                )
                continue

            price = pricing.rds_hourly(database.instance_class, database.multi_az, inventory.region)
            storage = pricing.rds_storage_monthly(
                database.storage_type, database.allocated_storage_gib, inventory.region
            )
            instance_monthly = pricing.monthly(price.amount) if price else 0.0
            age = (
                age_days(config.now, database.create_time)
                if database.create_time is not None
                else None
            )
            if price is None:
                confidence = Confidence.LOW
            elif (
                database.avg_connections == 0
                and (age is None or age >= config.lookback_days)
                and not price.approximate
            ):
                confidence = Confidence.HIGH
            else:
                confidence = Confidence.MEDIUM

            cpu_text = (
                f" and {database.avg_cpu_percent:.1f}% CPU"
                if database.avg_cpu_percent is not None
                else ""
            )
            findings.append(
                Finding(
                    check_id=self.id,
                    title=self.title,
                    resource_id=database.db_instance_id,
                    resource_type="rds:db-instance",
                    region=inventory.region,
                    monthly_cost=instance_monthly + storage.amount,
                    estimated_monthly_savings=instance_monthly,
                    confidence=confidence,
                    reason=(
                        f"{database.instance_class} {database.engine} averaged "
                        f"{database.avg_connections:.2f} connections{cpu_text} over the last "
                        f"{config.lookback_days} days"
                    ),
                    recommendation=(
                        "Stop the instance (RDS restarts it after 7 days, so schedule it) or "
                        "take a final snapshot and delete it."
                    ),
                    details={
                        "instance_class": database.instance_class,
                        "engine": database.engine,
                        "multi_az": database.multi_az,
                        "avg_connections": round(database.avg_connections, 3),
                        "avg_cpu_percent": round(database.avg_cpu_percent, 2)
                        if database.avg_cpu_percent is not None
                        else None,
                        "allocated_storage_gib": database.allocated_storage_gib,
                        "storage_monthly": round(storage.amount, 2),
                        "age_days": round(age, 1) if age is not None else None,
                        "price_note": price.note if price else "no bundled price",
                    },
                )
            )
        return findings
