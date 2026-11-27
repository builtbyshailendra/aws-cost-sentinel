"""Network checks: unused Elastic IPs, idle load balancers and NAT gateway spend."""

from __future__ import annotations

import logging

from cost_sentinel import pricing
from cost_sentinel.checks.base import BaseCheck, age_days, fmt_money, per_month
from cost_sentinel.models import Confidence, Finding, Inventory, ScanConfig

logger = logging.getLogger(__name__)

BYTES_PER_GB = 1_000_000_000
"""NAT gateway data processing is billed per decimal gigabyte."""


class UnusedElasticIpCheck(BaseCheck):
    """Elastic IPs that are not associated, or sit on a stopped instance."""

    id = "unused-eip"
    title = "Unused Elastic IPs"
    description = (
        "Every public IPv4 address is billed hourly. Unassociated addresses are pure waste; "
        "addresses attached to a stopped instance are reported with medium confidence."
    )

    def run(self, inventory: Inventory, config: ScanConfig) -> list[Finding]:
        monthly = pricing.elastic_ip_monthly(inventory.region).amount
        findings: list[Finding] = []
        for address in inventory.elastic_ips:
            if address.association_id is None:
                confidence = Confidence.HIGH
                reason = f"{address.public_ip} is not associated with any resource"
                recommendation = (
                    "Release the address, or associate it with the resource that needs it."
                )
            elif address.instance_id and address.instance_state == "stopped":
                confidence = Confidence.MEDIUM
                reason = (
                    f"{address.public_ip} is attached to stopped instance {address.instance_id}"
                )
                recommendation = (
                    "Release the address if the instance stays stopped; it is billed while the "
                    "instance is off."
                )
            else:
                continue
            findings.append(
                Finding(
                    check_id=self.id,
                    title=self.title,
                    resource_id=address.allocation_id,
                    resource_type="ec2:elastic-ip",
                    region=inventory.region,
                    monthly_cost=monthly,
                    estimated_monthly_savings=monthly,
                    confidence=confidence,
                    reason=reason,
                    recommendation=recommendation,
                    details={
                        "public_ip": address.public_ip,
                        "instance_id": address.instance_id,
                        "instance_state": address.instance_state,
                        "network_interface_id": address.network_interface_id,
                    },
                )
            )
        return findings


class IdleLoadBalancerCheck(BaseCheck):
    """Load balancers with no healthy target behind them."""

    id = "idle-load-balancer"
    title = "Load balancers with no healthy targets"
    description = (
        "A load balancer with zero healthy targets cannot serve traffic but still bills its "
        "hourly charge. Zero registered targets means high confidence; registered but "
        "unhealthy targets may be a transient outage, so medium."
    )

    def run(self, inventory: Inventory, config: ScanConfig) -> list[Finding]:
        findings: list[Finding] = []
        for balancer in inventory.load_balancers:
            registered = sum(group.registered_targets for group in balancer.target_groups)
            healthy = sum(group.healthy_targets for group in balancer.target_groups)
            if healthy > 0:
                continue
            price = pricing.elb_monthly(balancer.lb_type, inventory.region)
            age = age_days(config.now, balancer.created_time)
            if registered == 0:
                confidence = Confidence.HIGH if age >= config.lookback_days else Confidence.MEDIUM
                reason = (
                    f"{balancer.lb_type} load balancer has no registered targets across "
                    f"{len(balancer.target_groups)} target group(s)"
                )
            else:
                confidence = Confidence.MEDIUM
                reason = f"{registered} registered target(s), none healthy"
            findings.append(
                Finding(
                    check_id=self.id,
                    title=self.title,
                    resource_id=balancer.name or balancer.arn,
                    resource_type=f"elbv2:{balancer.lb_type}",
                    region=inventory.region,
                    monthly_cost=price.amount,
                    estimated_monthly_savings=price.amount,
                    confidence=confidence,
                    reason=reason,
                    recommendation=(
                        "Delete the load balancer if the service behind it is gone; otherwise "
                        "fix the target group health checks."
                    ),
                    details={
                        "arn": balancer.arn,
                        "type": balancer.lb_type,
                        "scheme": balancer.scheme,
                        "age_days": round(age, 1),
                        "registered_targets": registered,
                        "target_groups": [group.name for group in balancer.target_groups],
                    },
                )
            )
        return findings


class NatGatewayCheck(BaseCheck):
    """NAT gateways that are idle, or whose data processing dwarfs the hourly fee."""

    id = "nat-gateway"
    title = "NAT gateway spend"
    description = (
        "Idle NAT gateways (little or no traffic over the lookback) cost the full hourly "
        "charge for nothing. Busy gateways whose processing charge exceeds the hourly charge "
        "usually carry S3, DynamoDB or other AWS traffic that VPC endpoints would carry for "
        "less; the saving assumes a configurable share of that traffic moves."
    )

    def run(self, inventory: Inventory, config: ScanConfig) -> list[Finding]:
        hourly_monthly = pricing.nat_gateway_monthly(inventory.region).amount
        findings: list[Finding] = []
        for gateway in inventory.nat_gateways:
            if gateway.state != "available":
                continue
            if gateway.bytes_processed is None:
                gigabytes: float | None = None
                processing_monthly = 0.0
            else:
                gigabytes = gateway.bytes_processed / BYTES_PER_GB
                processing_monthly = pricing.nat_gateway_processing(
                    per_month(gigabytes, config.lookback_days), inventory.region
                ).amount
            monthly_cost = hourly_monthly + processing_monthly
            details = {
                "vpc_id": gateway.vpc_id,
                "subnet_id": gateway.subnet_id,
                "gb_processed_in_window": round(gigabytes, 3) if gigabytes is not None else None,
                "hourly_charge_monthly": round(hourly_monthly, 2),
                "processing_charge_monthly": round(processing_monthly, 2),
            }
            if gigabytes is None:
                findings.append(
                    self._finding(
                        gateway.nat_gateway_id,
                        inventory.region,
                        monthly_cost,
                        hourly_monthly,
                        Confidence.MEDIUM,
                        reason=(
                            f"no data-processing metrics in the last {config.lookback_days} days, "
                            "which usually means no traffic"
                        ),
                        recommendation=(
                            "Confirm nothing routes through the gateway, then delete it; "
                            "re-create it when a private subnet needs internet access."
                        ),
                        details=details,
                    )
                )
            elif gigabytes < config.nat_idle_gb_threshold:
                findings.append(
                    self._finding(
                        gateway.nat_gateway_id,
                        inventory.region,
                        monthly_cost,
                        hourly_monthly,
                        Confidence.HIGH,
                        reason=(
                            f"only {gigabytes:.2f} GB processed in the last "
                            f"{config.lookback_days} days (idle threshold "
                            f"{config.nat_idle_gb_threshold:g} GB)"
                        ),
                        recommendation=(
                            "Delete the gateway, or replace it with VPC endpoints if the "
                            "subnet only needs to reach AWS services."
                        ),
                        details=details,
                    )
                )
            elif processing_monthly > hourly_monthly:
                savings = processing_monthly * config.nat_endpoint_savings_ratio
                findings.append(
                    self._finding(
                        gateway.nat_gateway_id,
                        inventory.region,
                        monthly_cost,
                        savings,
                        Confidence.LOW,
                        reason=(
                            f"data processing costs about {fmt_money(processing_monthly)} per "
                            f"month, more than the {fmt_money(hourly_monthly)} hourly charge"
                        ),
                        recommendation=(
                            "Add gateway endpoints for S3 and DynamoDB (free) and interface "
                            "endpoints for the AWS services that dominate the traffic, and keep "
                            "cross-AZ traffic inside the AZ."
                        ),
                        details={
                            **details,
                            "endpoint_savings_ratio": config.nat_endpoint_savings_ratio,
                        },
                    )
                )
        return findings

    def _finding(
        self,
        nat_id: str,
        region: str,
        monthly_cost: float,
        savings: float,
        confidence: Confidence,
        *,
        reason: str,
        recommendation: str,
        details: dict[str, object],
    ) -> Finding:
        return Finding(
            check_id=self.id,
            title=self.title,
            resource_id=nat_id,
            resource_type="ec2:nat-gateway",
            region=region,
            monthly_cost=monthly_cost,
            estimated_monthly_savings=savings,
            confidence=confidence,
            reason=reason,
            recommendation=recommendation,
            details=details,
        )
