"""Data model shared by collectors, checks and report writers.

Everything here is a plain dataclass so the scanner can be driven from live AWS
data, from a Cost Explorer CSV export, or from hand-built fixtures in tests
without any boto3 object leaking through the rest of the code base.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

HOURS_PER_MONTH = 730.0
"""Hours in an average month; the convention AWS pricing pages use."""


class Confidence(StrEnum):
    """How sure a check is that the savings estimate is real."""

    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"

    @property
    def rank(self) -> int:
        """Numeric rank used for sorting (higher is more confident)."""
        return {"high": 3, "medium": 2, "low": 1}[self.value]


class CostSentinelError(Exception):
    """Base class for errors raised by this package."""


@dataclass(frozen=True)
class Finding:
    """One piece of waste found by a check."""

    check_id: str
    title: str
    resource_id: str
    resource_type: str
    region: str
    monthly_cost: float
    estimated_monthly_savings: float
    confidence: Confidence
    reason: str
    recommendation: str
    details: dict[str, Any] = field(default_factory=dict)
    counted_in_total: bool = True
    """False when another finding on the same resource already counts a bigger saving.

    Set by the scanner (see :func:`cost_sentinel.scanner.resolve_overlaps`); the
    finding stays in the report so the alternative fix is still visible.
    """

    @property
    def resource_key(self) -> tuple[str, str, str]:
        """Identity of the billed resource: two findings with the same key overlap."""
        return (self.region, self.resource_type, self.resource_id)

    def to_dict(self) -> dict[str, Any]:
        """Serialise for the JSON writer (floats rounded to cents)."""
        return {
            "check_id": self.check_id,
            "title": self.title,
            "resource_id": self.resource_id,
            "resource_type": self.resource_type,
            "region": self.region,
            "monthly_cost": round(self.monthly_cost, 2),
            "estimated_monthly_savings": round(self.estimated_monthly_savings, 2),
            "confidence": self.confidence.value,
            "reason": self.reason,
            "recommendation": self.recommendation,
            "counted_in_total": self.counted_in_total,
            "details": dict(self.details),
        }


# --------------------------------------------------------------------------------------
# Inventory: what the collectors produce and the checks consume
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Instance:
    """An EC2 instance plus the CloudWatch CPU average over the lookback window."""

    instance_id: str
    instance_type: str
    state: str
    launch_time: datetime
    avg_cpu_percent: float | None
    lifecycle: str = "on-demand"
    platform: str = "Linux/UNIX"
    """EC2 ``PlatformDetails`` (``Linux/UNIX``, ``Windows``, ``Red Hat Enterprise Linux``...)."""
    name: str = ""
    tags: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class Volume:
    """An EBS volume."""

    volume_id: str
    size_gib: int
    volume_type: str
    state: str
    create_time: datetime
    attached_instance_ids: tuple[str, ...] = ()
    iops: int | None = None
    throughput_mbps: int | None = None
    name: str = ""


@dataclass(frozen=True)
class Snapshot:
    """An EBS snapshot owned by the account."""

    snapshot_id: str
    volume_id: str
    volume_size_gib: int
    start_time: datetime
    description: str = ""
    ami_id: str | None = None
    """Set when an AMI owned by the account references this snapshot."""


@dataclass(frozen=True)
class ElasticIp:
    """A public IPv4 Elastic IP allocation."""

    allocation_id: str
    public_ip: str
    association_id: str | None
    instance_id: str | None
    network_interface_id: str | None
    instance_state: str | None = None


@dataclass(frozen=True)
class TargetGroupHealth:
    """Health summary for one target group behind a load balancer."""

    target_group_arn: str
    name: str
    registered_targets: int
    healthy_targets: int


@dataclass(frozen=True)
class LoadBalancer:
    """An ELBv2 load balancer (application, network or gateway)."""

    arn: str
    name: str
    lb_type: str
    scheme: str
    created_time: datetime
    target_groups: tuple[TargetGroupHealth, ...] = ()


@dataclass(frozen=True)
class NatGateway:
    """A NAT gateway plus bytes processed over the lookback window."""

    nat_gateway_id: str
    vpc_id: str
    subnet_id: str
    state: str
    bytes_processed: float | None
    """Sum of BytesOutToDestination + BytesOutToSource over the lookback; None if unknown."""


@dataclass(frozen=True)
class DbInstance:
    """An RDS database instance plus CloudWatch connection and CPU averages."""

    db_instance_id: str
    instance_class: str
    engine: str
    status: str
    multi_az: bool
    allocated_storage_gib: int
    storage_type: str
    avg_connections: float | None
    avg_cpu_percent: float | None
    create_time: datetime | None = None


@dataclass(frozen=True)
class Bucket:
    """An S3 bucket with lifecycle and size information."""

    name: str
    region: str
    has_lifecycle_rules: bool
    size_gb: float | None
    """StandardStorage size from CloudWatch; None when the metric is unavailable."""


@dataclass
class CostReport:
    """Monthly cost per service, from a Cost Explorer CSV or the Cost Explorer API.

    ``periods`` are ``YYYY-MM`` strings in ascending order.  ``services`` maps a
    service name to a list of costs aligned with ``periods``.
    """

    periods: list[str]
    services: dict[str, list[float]]
    currency: str = "USD"
    source: str = ""

    def __post_init__(self) -> None:
        for name, values in self.services.items():
            if len(values) != len(self.periods):
                msg = (
                    f"service {name!r} has {len(values)} values "
                    f"but there are {len(self.periods)} periods"
                )
                raise ValueError(msg)

    @property
    def months(self) -> int:
        """Number of periods in the report."""
        return len(self.periods)

    def total_by_period(self) -> list[float]:
        """Total spend per period across all services."""
        return [
            sum(values[index] for values in self.services.values())
            for index in range(len(self.periods))
        ]

    def grand_total(self) -> float:
        """Total spend over every period and service."""
        return sum(self.total_by_period())

    def service_total(self, service: str) -> float:
        """Total spend for one service across all periods."""
        return sum(self.services[service])

    def latest(self, service: str) -> float:
        """Spend of the most recent period for one service."""
        return self.services[service][-1]

    def trailing_average(self, service: str) -> float | None:
        """Average of all periods except the latest; None when fewer than two periods."""
        values = self.services[service][:-1]
        if not values:
            return None
        return sum(values) / len(values)

    def top_services(self, limit: int = 10) -> list[tuple[str, float]]:
        """Services sorted by total spend, highest first."""
        ranked = sorted(self.services.items(), key=lambda item: sum(item[1]), reverse=True)
        return [(name, sum(values)) for name, values in ranked[:limit]]

    def find_service(self, *needles: str) -> str | None:
        """Return the first service whose name contains all ``needles`` (case-insensitive)."""
        lowered = [needle.lower() for needle in needles]
        for name in self.services:
            haystack = name.lower()
            if all(needle in haystack for needle in lowered):
                return name
        return None


@dataclass
class Inventory:
    """Everything the checks need, collected once per scan."""

    region: str
    collected_at: datetime
    instances: list[Instance] = field(default_factory=list)
    volumes: list[Volume] = field(default_factory=list)
    snapshots: list[Snapshot] = field(default_factory=list)
    elastic_ips: list[ElasticIp] = field(default_factory=list)
    load_balancers: list[LoadBalancer] = field(default_factory=list)
    nat_gateways: list[NatGateway] = field(default_factory=list)
    db_instances: list[DbInstance] = field(default_factory=list)
    buckets: list[Bucket] = field(default_factory=list)
    cost_report: CostReport | None = None
    errors: list[str] = field(default_factory=list)
    """Human-readable notes about services that could not be collected."""
    collected: list[str] = field(default_factory=list)
    """API labels (``ec2:DescribeInstances``...) that were collected successfully."""
    resource_data: bool = True
    """False when the inventory only carries cost data (CSV mode): resource checks skip."""

    def counts(self) -> dict[str, int]:
        """Resource counts for the report header."""
        return {
            "instances": len(self.instances),
            "volumes": len(self.volumes),
            "snapshots": len(self.snapshots),
            "elastic_ips": len(self.elastic_ips),
            "load_balancers": len(self.load_balancers),
            "nat_gateways": len(self.nat_gateways),
            "db_instances": len(self.db_instances),
            "buckets": len(self.buckets),
        }

    @classmethod
    def from_cost_report(cls, report: CostReport, region: str = "global") -> Inventory:
        """Build an inventory that only carries cost data (CSV mode)."""
        return cls(
            region=region,
            collected_at=datetime.now(UTC),
            cost_report=report,
            resource_data=False,
        )


# --------------------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------------------


@dataclass
class ScanConfig:
    """Thresholds and assumptions that drive the checks.

    Every value has a conservative default; the CLI and the GitHub Action expose the
    ones people change most often.  ``reference_time`` pins "now" so that reports and
    tests are reproducible.
    """

    region: str = "us-east-1"
    lookback_days: int = 14
    months: int = 6
    ec2_cpu_threshold: float = 5.0
    ebs_unattached_min_age_days: int = 7
    snapshot_max_age_days: int = 90
    snapshot_incremental_ratio: float = 0.5
    rds_connections_threshold: float = 1.0
    rds_cpu_threshold: float = 5.0
    nat_idle_gb_threshold: float = 1.0
    nat_endpoint_savings_ratio: float = 0.3
    s3_tierable_ratio: float = 0.3
    savings_plan_discount: float = 0.20
    savings_plan_min_uptime_days: int = 30
    spend_growth_threshold: float = 0.25
    min_savings: float = 0.0
    checks: tuple[str, ...] | None = None
    reference_time: datetime | None = None

    def __post_init__(self) -> None:
        if self.lookback_days < 1:
            raise ValueError("lookback_days must be at least 1")
        if self.months < 1:
            raise ValueError("months must be at least 1")
        for name in (
            "snapshot_incremental_ratio",
            "nat_endpoint_savings_ratio",
            "s3_tierable_ratio",
            "savings_plan_discount",
        ):
            value = getattr(self, name)
            if not 0.0 <= value <= 1.0:
                raise ValueError(f"{name} must be between 0 and 1, got {value}")

    @property
    def now(self) -> datetime:
        """The timestamp checks compare ages against."""
        return self.reference_time or datetime.now(UTC)

    def to_dict(self) -> dict[str, Any]:
        """Serialise the thresholds for the report metadata."""
        return {
            "region": self.region,
            "lookback_days": self.lookback_days,
            "months": self.months,
            "ec2_cpu_threshold": self.ec2_cpu_threshold,
            "ebs_unattached_min_age_days": self.ebs_unattached_min_age_days,
            "snapshot_max_age_days": self.snapshot_max_age_days,
            "snapshot_incremental_ratio": self.snapshot_incremental_ratio,
            "rds_connections_threshold": self.rds_connections_threshold,
            "rds_cpu_threshold": self.rds_cpu_threshold,
            "nat_idle_gb_threshold": self.nat_idle_gb_threshold,
            "nat_endpoint_savings_ratio": self.nat_endpoint_savings_ratio,
            "s3_tierable_ratio": self.s3_tierable_ratio,
            "savings_plan_discount": self.savings_plan_discount,
            "savings_plan_min_uptime_days": self.savings_plan_min_uptime_days,
            "spend_growth_threshold": self.spend_growth_threshold,
            "min_savings": self.min_savings,
            "checks": list(self.checks) if self.checks else None,
        }


# --------------------------------------------------------------------------------------
# Scan result
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class CheckSummary:
    """Per-check roll-up used in the report summary table."""

    check_id: str
    title: str
    findings: int
    estimated_monthly_savings: float


@dataclass
class ScanResult:
    """Findings plus the metadata the report writers need."""

    findings: list[Finding]
    inventory: Inventory
    config: ScanConfig
    generated_at: datetime
    source: str
    """``live`` for a boto3 scan, ``csv`` for a Cost Explorer export."""
    source_detail: str = ""
    """Profile name, or the CSV path."""
    check_titles: dict[str, str] = field(default_factory=dict)
    skipped_checks: dict[str, str] = field(default_factory=dict)
    """Selected checks that could not run, mapped to the reason (e.g. CSV input)."""

    @property
    def total_savings(self) -> float:
        """Estimated monthly savings with every resource counted once.

        Findings marked ``counted_in_total=False`` describe an alternative fix for a
        resource another finding already covers, so their savings are left out.
        """
        return sum(f.estimated_monthly_savings for f in self.findings if f.counted_in_total)

    @property
    def gross_savings(self) -> float:
        """Plain sum over every finding, including overlapping alternatives."""
        return sum(finding.estimated_monthly_savings for finding in self.findings)

    @property
    def overlapping_savings(self) -> float:
        """Savings excluded from :attr:`total_savings` because they overlap."""
        return self.gross_savings - self.total_savings

    def by_confidence(self) -> dict[str, int]:
        """Finding counts keyed by confidence level."""
        counts = {level.value: 0 for level in Confidence}
        for finding in self.findings:
            counts[finding.confidence.value] += 1
        return counts

    def summaries(self) -> list[CheckSummary]:
        """One row per check that produced findings, biggest savings first.

        Savings per row only include counted findings, so the rows add up to
        :attr:`total_savings`.
        """
        grouped: dict[str, list[Finding]] = {}
        for finding in self.findings:
            grouped.setdefault(finding.check_id, []).append(finding)
        rows = [
            CheckSummary(
                check_id=check_id,
                title=self.check_titles.get(check_id, check_id),
                findings=len(items),
                estimated_monthly_savings=sum(
                    f.estimated_monthly_savings for f in items if f.counted_in_total
                ),
            )
            for check_id, items in grouped.items()
        ]
        return sorted(rows, key=lambda row: row.estimated_monthly_savings, reverse=True)

    def findings_for(self, check_id: str) -> list[Finding]:
        """Findings of one check, largest savings first."""
        items = [f for f in self.findings if f.check_id == check_id]
        return sorted(items, key=lambda f: f.estimated_monthly_savings, reverse=True)

    def to_dict(self) -> dict[str, Any]:
        """Serialise the whole result (used by the JSON writer)."""
        return {
            "schema_version": 1,
            "generated_at": self.generated_at.isoformat(),
            "source": self.source,
            "source_detail": self.source_detail,
            "region": self.inventory.region,
            "summary": {
                "finding_count": len(self.findings),
                "estimated_monthly_savings": round(self.total_savings, 2),
                "overlapping_savings_excluded": round(self.overlapping_savings, 2),
                "by_confidence": self.by_confidence(),
                "by_check": [
                    {
                        "check_id": row.check_id,
                        "title": row.title,
                        "findings": row.findings,
                        "estimated_monthly_savings": round(row.estimated_monthly_savings, 2),
                    }
                    for row in self.summaries()
                ],
                "resources": self.inventory.counts(),
            },
            "config": self.config.to_dict(),
            "errors": list(self.inventory.errors),
            "skipped_checks": dict(self.skipped_checks),
            "findings": [finding.to_dict() for finding in self.findings],
        }
