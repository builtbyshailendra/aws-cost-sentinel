"""EC2 compute checks: idle instances and Savings Plan coverage."""

from __future__ import annotations

import logging

from cost_sentinel import pricing
from cost_sentinel.checks.base import BaseCheck, age_days, fmt_money
from cost_sentinel.models import Confidence, Finding, Instance, Inventory, ScanConfig

logger = logging.getLogger(__name__)


def is_idle_instance(instance: Instance, config: ScanConfig) -> bool:
    """True when ``instance`` meets the idle-ec2 criteria.

    Shared by :class:`IdleEc2Check` and :class:`SavingsPlanCheck`, so an instance the
    report recommends stopping is never also counted in the Savings Plan baseline.
    """
    return (
        instance.state == "running"
        and instance.lifecycle != "spot"
        and instance.avg_cpu_percent is not None
        and instance.avg_cpu_percent < config.ec2_cpu_threshold
    )


class IdleEc2Check(BaseCheck):
    """Running on-demand instances whose CPU stayed under the threshold."""

    id = "idle-ec2"
    title = "Idle EC2 instances"
    description = (
        "Running on-demand instances whose average CPU over the lookback window is below "
        "the configured threshold. Spot instances and instances without CloudWatch data "
        "are skipped. Windows instances include the licence premium."
    )

    def run(self, inventory: Inventory, config: ScanConfig) -> list[Finding]:
        findings: list[Finding] = []
        for instance in inventory.instances:
            if instance.state == "running" and instance.avg_cpu_percent is None:
                logger.debug("%s has no CPU datapoints; skipping", instance.instance_id)
            if not is_idle_instance(instance, config):
                continue
            avg_cpu = instance.avg_cpu_percent or 0.0

            uptime = age_days(config.now, instance.launch_time)
            if uptime < config.lookback_days:
                confidence = Confidence.LOW
            elif avg_cpu < config.ec2_cpu_threshold / 2:
                confidence = Confidence.HIGH
            else:
                confidence = Confidence.MEDIUM

            price = pricing.ec2_hourly(instance.instance_type, inventory.region, instance.platform)
            if price is None:
                monthly_cost = 0.0
                confidence = Confidence.LOW
                price_note = "no bundled price for this instance type"
            else:
                monthly_cost = pricing.monthly(price.amount)
                price_note = price.note
                if price.approximate and confidence is Confidence.HIGH:
                    confidence = Confidence.MEDIUM

            label = f" ({instance.name})" if instance.name else ""
            findings.append(
                Finding(
                    check_id=self.id,
                    title=self.title,
                    resource_id=instance.instance_id,
                    resource_type="ec2:instance",
                    region=inventory.region,
                    monthly_cost=monthly_cost,
                    estimated_monthly_savings=monthly_cost,
                    confidence=confidence,
                    reason=(
                        f"{instance.instance_type}{label} averaged "
                        f"{avg_cpu:.1f}% CPU over the last "
                        f"{config.lookback_days} days (threshold {config.ec2_cpu_threshold:g}%)"
                    ),
                    recommendation=(
                        "Stop the instance if nobody owns it, otherwise move it to a smaller "
                        "type or schedule it to stop outside working hours."
                    ),
                    details={
                        "instance_type": instance.instance_type,
                        "platform": instance.platform,
                        "name": instance.name,
                        "avg_cpu_percent": round(avg_cpu, 2),
                        "uptime_days": round(uptime, 1),
                        "hourly_price": round(price.amount, 4) if price else None,
                        "price_note": price_note,
                    },
                )
            )
        return findings


class SavingsPlanCheck(BaseCheck):
    """Steady-state on-demand compute that a Compute Savings Plan would discount."""

    id = "savings-plan"
    title = "Savings Plan opportunity"
    description = (
        "On-demand instances running longer than the minimum uptime form a steady baseline; "
        "a 1-year no-upfront Compute Savings Plan discounts that baseline. Instances that "
        "meet the idle-ec2 criteria are excluded, because the better fix is to stop them. "
        "Falls back to the lowest recent month of EC2 spend when the inventory has no "
        "running on-demand instances (CSV mode)."
    )
    needs_inventory = False
    inputs = "inventory or cost data"

    def run(self, inventory: Inventory, config: ScanConfig) -> list[Finding]:
        on_demand = [
            instance
            for instance in inventory.instances
            if instance.state == "running" and instance.lifecycle == "on-demand"
        ]
        finding: Finding | None = None
        if on_demand:
            # The account-wide Cost Explorer baseline would include these instances
            # (idle or not), so it is only used when the inventory has no compute.
            finding = self._from_inventory(inventory, config, on_demand)
        elif inventory.cost_report is not None:
            finding = self._from_cost_report(inventory, config)
        return [finding] if finding else []

    def _from_inventory(
        self, inventory: Inventory, config: ScanConfig, on_demand: list[Instance]
    ) -> Finding | None:
        idle = [instance for instance in on_demand if is_idle_instance(instance, config)]
        steady = [
            instance
            for instance in on_demand
            if not is_idle_instance(instance, config)
            and age_days(config.now, instance.launch_time) >= config.savings_plan_min_uptime_days
        ]
        if not steady:
            return None
        base_hourly = 0.0
        licence_hourly = 0.0
        unpriced: list[str] = []
        unmodelled_licences: list[str] = []
        approximate = False
        for instance in steady:
            price = pricing.ec2_hourly(instance.instance_type, inventory.region)
            if price is None:
                unpriced.append(instance.instance_id)
                continue
            approximate = approximate or price.approximate
            base_hourly += price.amount
            licence_hourly += pricing.ec2_licence_hourly(
                instance.instance_type, instance.platform, inventory.region
            )
            if pricing.platform_kind(instance.platform) in {"other-licensed", "windows-sql"}:
                unmodelled_licences.append(instance.instance_id)
        if base_hourly <= 0:
            return None
        # A Savings Plan discounts the compute rate; licence premiums are billed as before.
        monthly_cost = pricing.monthly(base_hourly + licence_hourly)
        savings = pricing.monthly(base_hourly) * config.savings_plan_discount
        confidence = (
            Confidence.LOW
            if (unpriced or approximate or unmodelled_licences)
            else Confidence.MEDIUM
        )
        commitment = base_hourly * (1 - config.savings_plan_discount)
        reason = (
            f"{len(steady)} on-demand instances have run for more than "
            f"{config.savings_plan_min_uptime_days} days and cost about "
            f"{fmt_money(monthly_cost)} per month"
        )
        if idle:
            reason += f"; {len(idle)} idle instance(s) excluded (see idle-ec2)"
        return Finding(
            check_id=self.id,
            title=self.title,
            resource_id=f"{len(steady)} steady-state instances",
            resource_type="savings-plan",
            region=inventory.region,
            monthly_cost=monthly_cost,
            estimated_monthly_savings=savings,
            confidence=confidence,
            reason=reason,
            recommendation=(
                f"Buy a 1-year, no-upfront Compute Savings Plan with an hourly commitment "
                f"of about {fmt_money(commitment)}; it applies across instance families, "
                f"sizes and regions, so the baseline can keep changing."
            ),
            details={
                "instances": [instance.instance_id for instance in steady],
                "excluded_idle_instances": [instance.instance_id for instance in idle],
                "unpriced_instances": unpriced,
                "unmodelled_licence_instances": unmodelled_licences,
                "on_demand_hourly": round(base_hourly, 4),
                "licence_hourly": round(licence_hourly, 4),
                "commitment_hourly": round(commitment, 4),
                "discount": config.savings_plan_discount,
            },
        )

    def _from_cost_report(self, inventory: Inventory, config: ScanConfig) -> Finding | None:
        report = inventory.cost_report
        if report is None:
            return None
        service = report.find_service("elastic compute cloud", "compute") or report.find_service(
            "ec2", "instances"
        )
        if service is None:
            return None
        values = report.services[service]
        recent = values[-3:]
        baseline = min(recent)
        if baseline <= 0:
            return None
        savings = baseline * config.savings_plan_discount
        confidence = Confidence.MEDIUM if len(recent) >= 3 else Confidence.LOW
        return Finding(
            check_id=self.id,
            title=self.title,
            resource_id=service,
            resource_type="savings-plan",
            region=inventory.region,
            monthly_cost=baseline,
            estimated_monthly_savings=savings,
            confidence=confidence,
            reason=(
                f"{service} spend never dropped below {fmt_money(baseline)} per month over "
                f"the last {len(recent)} months, which looks like steady on-demand usage"
            ),
            recommendation=(
                "Check Cost Explorer's Savings Plans recommendations and commit to the "
                "baseline with a 1-year, no-upfront Compute Savings Plan."
            ),
            details={
                "service": service,
                "months_considered": len(recent),
                "baseline_monthly": round(baseline, 2),
                "discount": config.savings_plan_discount,
            },
        )
