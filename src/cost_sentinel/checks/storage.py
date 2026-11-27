"""Storage checks: unattached EBS, gp2 volumes, old snapshots and S3 lifecycle rules."""

from __future__ import annotations

import logging

from cost_sentinel import pricing
from cost_sentinel.checks.base import BaseCheck, age_days, fmt_money
from cost_sentinel.models import Confidence, Finding, Inventory, ScanConfig

logger = logging.getLogger(__name__)

MIN_BUCKET_GB = 1.0
"""Buckets smaller than this are not reported for missing lifecycle rules."""


class UnattachedEbsCheck(BaseCheck):
    """EBS volumes in the ``available`` state (not attached to anything)."""

    id = "unattached-ebs"
    title = "Unattached EBS volumes"
    description = (
        "Volumes in the 'available' state are billed in full while attached to nothing. "
        "Confidence is high once the volume is older than the minimum age."
    )

    def run(self, inventory: Inventory, config: ScanConfig) -> list[Finding]:
        findings: list[Finding] = []
        for volume in inventory.volumes:
            if volume.state != "available":
                continue
            age = age_days(config.now, volume.create_time)
            price = pricing.ebs_monthly(
                volume.volume_type,
                volume.size_gib,
                volume.iops,
                volume.throughput_mbps,
                inventory.region,
            )
            confidence = (
                Confidence.HIGH if age >= config.ebs_unattached_min_age_days else Confidence.MEDIUM
            )
            label = f" ({volume.name})" if volume.name else ""
            findings.append(
                Finding(
                    check_id=self.id,
                    title=self.title,
                    resource_id=volume.volume_id,
                    resource_type="ec2:volume",
                    region=inventory.region,
                    monthly_cost=price.amount,
                    estimated_monthly_savings=price.amount,
                    confidence=confidence,
                    reason=(
                        f"{volume.size_gib} GiB {volume.volume_type}{label} has no attachment "
                        f"and was created {age:.0f} days ago"
                    ),
                    recommendation=("Take a snapshot if the data matters, then delete the volume."),
                    details={
                        "size_gib": volume.size_gib,
                        "volume_type": volume.volume_type,
                        "age_days": round(age, 1),
                        "name": volume.name,
                    },
                )
            )
        return findings


class Gp2ToGp3Check(BaseCheck):
    """gp2 volumes that would be cheaper (and faster) as gp3."""

    id = "gp2-to-gp3"
    title = "gp2 volumes convertible to gp3"
    description = (
        "gp3 costs 20% less per GiB and includes 3000 IOPS and 125 MiB/s. Volumes over "
        "1000 GiB need provisioned IOPS on gp3 to keep their gp2 baseline, which the "
        "estimate accounts for. Unattached volumes are left to unattached-ebs: deleting "
        "them saves the full price, so converting them first would be counted twice."
    )

    def run(self, inventory: Inventory, config: ScanConfig) -> list[Finding]:
        findings: list[Finding] = []
        for volume in inventory.volumes:
            if volume.volume_type != "gp2" or volume.size_gib <= 0:
                continue
            if volume.state == "available":
                logger.debug("%s is unattached; reported by unattached-ebs", volume.volume_id)
                continue
            current = pricing.ebs_monthly("gp2", volume.size_gib, region=inventory.region)
            target = pricing.gp3_equivalent_monthly(volume.size_gib, inventory.region)
            savings = current.amount - target.amount
            if savings <= 0:
                logger.debug("%s would not save money on gp3", volume.volume_id)
                continue
            needed_iops = pricing.gp2_baseline_iops(volume.size_gib)
            gp3_iops = max(needed_iops, pricing.GP3_BASELINE_IOPS)
            if needed_iops <= pricing.GP3_BASELINE_IOPS:
                confidence = Confidence.HIGH
                recommendation = (
                    "Run ModifyVolume to gp3 (online, no downtime); the included 3000 IOPS "
                    f"and 125 MiB/s already exceed the gp2 baseline of {needed_iops} IOPS."
                )
            else:
                confidence = Confidence.MEDIUM
                recommendation = (
                    f"Run ModifyVolume to gp3 (online, no downtime) with {needed_iops} "
                    "provisioned IOPS to keep the gp2 baseline, and raise throughput above "
                    "125 MiB/s only if CloudWatch shows the volume needs it."
                )
            findings.append(
                Finding(
                    check_id=self.id,
                    title=self.title,
                    resource_id=volume.volume_id,
                    resource_type="ec2:volume",
                    region=inventory.region,
                    monthly_cost=current.amount,
                    estimated_monthly_savings=savings,
                    confidence=confidence,
                    reason=(
                        f"{volume.size_gib} GiB gp2 costs {fmt_money(current.amount)}/month; "
                        f"as gp3 with {gp3_iops} IOPS it costs {fmt_money(target.amount)}/month"
                    ),
                    recommendation=recommendation,
                    details={
                        "size_gib": volume.size_gib,
                        "gp2_monthly": round(current.amount, 2),
                        "gp3_monthly": round(target.amount, 2),
                        "required_iops": needed_iops,
                        "gp3_iops": gp3_iops,
                        "state": volume.state,
                    },
                )
            )
        return findings


class OldSnapshotCheck(BaseCheck):
    """EBS snapshots older than the configured age that no AMI depends on."""

    id = "old-snapshots"
    title = "Old EBS snapshots"
    description = (
        "Snapshots older than the maximum age. Snapshots referenced by an AMI are skipped "
        "because deleting them breaks the image. Savings assume an incremental snapshot "
        "holds a fraction of the source volume (configurable)."
    )

    def run(self, inventory: Inventory, config: ScanConfig) -> list[Finding]:
        known_volumes = {volume.volume_id for volume in inventory.volumes}
        findings: list[Finding] = []
        for snapshot in inventory.snapshots:
            age = age_days(config.now, snapshot.start_time)
            if age < config.snapshot_max_age_days:
                continue
            if snapshot.ami_id:
                logger.debug("%s backs %s; skipping", snapshot.snapshot_id, snapshot.ami_id)
                continue
            full_price = pricing.snapshot_monthly(snapshot.volume_size_gib, inventory.region)
            savings = full_price.amount * config.snapshot_incremental_ratio
            orphaned = bool(snapshot.volume_id) and snapshot.volume_id not in known_volumes
            confidence = Confidence.MEDIUM if orphaned else Confidence.LOW
            reason = (
                f"snapshot of {snapshot.volume_size_gib} GiB volume {snapshot.volume_id or 'n/a'} "
                f"is {age:.0f} days old"
            )
            if orphaned:
                reason += " and the source volume no longer exists"
            findings.append(
                Finding(
                    check_id=self.id,
                    title=self.title,
                    resource_id=snapshot.snapshot_id,
                    resource_type="ec2:snapshot",
                    region=inventory.region,
                    monthly_cost=full_price.amount,
                    estimated_monthly_savings=savings,
                    confidence=confidence,
                    reason=reason,
                    recommendation=(
                        "Delete it, or move it to the EBS Snapshots Archive tier (75% cheaper) "
                        "if it must be kept for compliance. Use Data Lifecycle Manager to "
                        "expire future snapshots automatically."
                    ),
                    details={
                        "age_days": round(age, 1),
                        "volume_id": snapshot.volume_id,
                        "volume_size_gib": snapshot.volume_size_gib,
                        "source_volume_exists": not orphaned,
                        "description": snapshot.description,
                        "incremental_ratio": config.snapshot_incremental_ratio,
                    },
                )
            )
        return findings


class S3LifecycleCheck(BaseCheck):
    """Buckets with no enabled lifecycle rule."""

    id = "s3-lifecycle"
    title = "S3 buckets without lifecycle rules"
    description = (
        "Buckets with no enabled lifecycle rule keep every object, version and failed "
        "multipart upload in Standard storage forever. Savings assume a configurable share "
        "of the data could move to Standard-IA; the bucket size comes from CloudWatch."
    )

    def run(self, inventory: Inventory, config: ScanConfig) -> list[Finding]:
        findings: list[Finding] = []
        for bucket in inventory.buckets:
            if bucket.has_lifecycle_rules:
                continue
            if bucket.size_gb is not None and bucket.size_gb < MIN_BUCKET_GB:
                logger.debug("%s is smaller than %s GB; skipping", bucket.name, MIN_BUCKET_GB)
                continue
            if bucket.size_gb is None:
                monthly_cost = 0.0
                savings = 0.0
                reason = "no lifecycle rules; size unknown (no CloudWatch storage metrics)"
            else:
                monthly_cost = pricing.s3_standard_monthly(bucket.size_gb, bucket.region).amount
                savings = (
                    bucket.size_gb
                    * pricing.s3_tiering_saving_per_gb(bucket.region)
                    * config.s3_tierable_ratio
                )
                reason = f"no lifecycle rules; {bucket.size_gb:,.1f} GB in Standard storage"
            findings.append(
                Finding(
                    check_id=self.id,
                    title=self.title,
                    resource_id=bucket.name,
                    resource_type="s3:bucket",
                    region=bucket.region,
                    monthly_cost=monthly_cost,
                    estimated_monthly_savings=savings,
                    confidence=Confidence.LOW,
                    reason=reason,
                    recommendation=(
                        "Add lifecycle rules: abort incomplete multipart uploads after 7 days, "
                        "transition objects to Standard-IA or Intelligent-Tiering after 30 days, "
                        "and expire old noncurrent versions."
                    ),
                    details={
                        "size_gb": round(bucket.size_gb, 2) if bucket.size_gb is not None else None,
                        "tierable_ratio": config.s3_tierable_ratio,
                    },
                )
            )
        return findings
