"""Collect the resource inventory a scan needs from a live AWS account.

Each ``collect_*`` function turns boto3 responses into the frozen dataclasses in
:mod:`cost_sentinel.models`.  :func:`collect_inventory` runs all of them, catching
permission and API errors per service so that one missing IAM permission degrades
the report instead of aborting the scan.

Degrading is only safe while something was collected. Missing or invalid credentials
abort the scan at the first call, and a scan in which every resource collector
failed raises :class:`CollectionError`, so a broken CI role can never produce a
clean "no waste found" report that passes a ``--fail-above`` gate.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from botocore.exceptions import (
    BotoCoreError,
    ClientError,
    CredentialRetrievalError,
    NoCredentialsError,
    PartialCredentialsError,
    TokenRetrievalError,
)

from cost_sentinel.aws.cost_explorer import CostExplorerError, fetch_cost_report
from cost_sentinel.aws.metrics import MetricReader
from cost_sentinel.aws.session import AwsClients
from cost_sentinel.models import (
    Bucket,
    CostSentinelError,
    DbInstance,
    ElasticIp,
    Instance,
    Inventory,
    LoadBalancer,
    NatGateway,
    ScanConfig,
    Snapshot,
    TargetGroupHealth,
    Volume,
)

logger = logging.getLogger(__name__)

BYTES_PER_GIB = 1024**3

CREDENTIAL_ERROR_CODES = frozenset(
    {
        "AuthFailure",
        "ExpiredToken",
        "ExpiredTokenException",
        "InvalidAccessKeyId",
        "InvalidClientTokenId",
        "InvalidToken",
        "RequestExpired",
        "SignatureDoesNotMatch",
        "UnrecognizedClientException",
    }
)
"""API error codes that mean the credentials are wrong, not that one permission is missing."""


class CollectionError(CostSentinelError):
    """The scan could not collect enough data to produce a trustworthy report."""


def is_credential_error(exc: Exception) -> bool:
    """True when ``exc`` says the credentials are missing, expired or invalid."""
    if isinstance(
        exc,
        NoCredentialsError
        | PartialCredentialsError
        | CredentialRetrievalError
        | TokenRetrievalError,
    ):
        return True
    if isinstance(exc, ClientError):
        return exc.response.get("Error", {}).get("Code", "") in CREDENTIAL_ERROR_CODES
    return False


def _tags(raw: list[dict[str, str]] | None) -> dict[str, str]:
    return {tag["Key"]: tag["Value"] for tag in raw or [] if "Key" in tag}


def _as_utc(value: datetime | None) -> datetime:
    if value is None:
        return datetime.now(UTC)
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


# ---------------------------------------------------------------------------
# EC2
# ---------------------------------------------------------------------------


def collect_instances(ec2: Any, metrics: MetricReader) -> list[Instance]:
    """All EC2 instances in the region with their average CPU (running ones only)."""
    instances: list[Instance] = []
    paginator = ec2.get_paginator("describe_instances")
    for page in paginator.paginate():
        for reservation in page.get("Reservations", []):
            for raw in reservation.get("Instances", []):
                state = raw.get("State", {}).get("Name", "unknown")
                instance_id = raw["InstanceId"]
                avg_cpu = None
                if state == "running":
                    avg_cpu = metrics.statistic(
                        "AWS/EC2", "CPUUtilization", {"InstanceId": instance_id}
                    )
                tags = _tags(raw.get("Tags"))
                instances.append(
                    Instance(
                        instance_id=instance_id,
                        instance_type=raw.get("InstanceType", "unknown"),
                        state=state,
                        launch_time=_as_utc(raw.get("LaunchTime")),
                        avg_cpu_percent=avg_cpu,
                        lifecycle=raw.get("InstanceLifecycle", "on-demand"),
                        platform=raw.get("PlatformDetails")
                        or ("Windows" if raw.get("Platform") == "windows" else "Linux/UNIX"),
                        name=tags.get("Name", ""),
                        tags=tags,
                    )
                )
    logger.info("collected %d EC2 instances", len(instances))
    return instances


def collect_volumes(ec2: Any) -> list[Volume]:
    """All EBS volumes in the region."""
    volumes: list[Volume] = []
    for page in ec2.get_paginator("describe_volumes").paginate():
        for raw in page.get("Volumes", []):
            attached = tuple(
                attachment["InstanceId"]
                for attachment in raw.get("Attachments", [])
                if attachment.get("InstanceId")
            )
            tags = _tags(raw.get("Tags"))
            volumes.append(
                Volume(
                    volume_id=raw["VolumeId"],
                    size_gib=int(raw.get("Size", 0)),
                    volume_type=raw.get("VolumeType", "unknown"),
                    state=raw.get("State", "unknown"),
                    create_time=_as_utc(raw.get("CreateTime")),
                    attached_instance_ids=attached,
                    iops=raw.get("Iops"),
                    throughput_mbps=raw.get("Throughput"),
                    name=tags.get("Name", ""),
                )
            )
    logger.info("collected %d EBS volumes", len(volumes))
    return volumes


def _ami_snapshot_map(ec2: Any) -> dict[str, str]:
    """Map snapshot id to the AMI (owned by this account) that references it."""
    mapping: dict[str, str] = {}
    for page in ec2.get_paginator("describe_images").paginate(Owners=["self"]):
        for image in page.get("Images", []):
            for device in image.get("BlockDeviceMappings", []):
                snapshot_id = device.get("Ebs", {}).get("SnapshotId")
                if snapshot_id:
                    mapping[snapshot_id] = image["ImageId"]
    return mapping


def collect_snapshots(ec2: Any) -> list[Snapshot]:
    """EBS snapshots owned by the account, flagged when an AMI depends on them."""
    ami_for_snapshot = _ami_snapshot_map(ec2)
    snapshots: list[Snapshot] = []
    for page in ec2.get_paginator("describe_snapshots").paginate(OwnerIds=["self"]):
        for raw in page.get("Snapshots", []):
            snapshot_id = raw["SnapshotId"]
            snapshots.append(
                Snapshot(
                    snapshot_id=snapshot_id,
                    volume_id=raw.get("VolumeId", ""),
                    volume_size_gib=int(raw.get("VolumeSize", 0)),
                    start_time=_as_utc(raw.get("StartTime")),
                    description=raw.get("Description", ""),
                    ami_id=ami_for_snapshot.get(snapshot_id),
                )
            )
    logger.info("collected %d EBS snapshots", len(snapshots))
    return snapshots


def collect_elastic_ips(ec2: Any, instance_states: dict[str, str]) -> list[ElasticIp]:
    """Elastic IP allocations with the state of the instance they point at."""
    response = ec2.describe_addresses()
    addresses: list[ElasticIp] = []
    for raw in response.get("Addresses", []):
        instance_id = raw.get("InstanceId") or None
        addresses.append(
            ElasticIp(
                allocation_id=raw.get("AllocationId", raw.get("PublicIp", "")),
                public_ip=raw.get("PublicIp", ""),
                association_id=raw.get("AssociationId") or None,
                instance_id=instance_id,
                network_interface_id=raw.get("NetworkInterfaceId") or None,
                instance_state=instance_states.get(instance_id) if instance_id else None,
            )
        )
    logger.info("collected %d Elastic IPs", len(addresses))
    return addresses


def collect_nat_gateways(ec2: Any, metrics: MetricReader) -> list[NatGateway]:
    """NAT gateways with the bytes they processed over the lookback window."""
    gateways: list[NatGateway] = []
    for page in ec2.get_paginator("describe_nat_gateways").paginate():
        for raw in page.get("NatGateways", []):
            nat_id = raw["NatGatewayId"]
            out_to_destination = metrics.statistic(
                "AWS/NATGateway", "BytesOutToDestination", {"NatGatewayId": nat_id}, "Sum"
            )
            out_to_source = metrics.statistic(
                "AWS/NATGateway", "BytesOutToSource", {"NatGatewayId": nat_id}, "Sum"
            )
            processed: float | None
            if out_to_destination is None and out_to_source is None:
                processed = None
            else:
                processed = (out_to_destination or 0.0) + (out_to_source or 0.0)
            gateways.append(
                NatGateway(
                    nat_gateway_id=nat_id,
                    vpc_id=raw.get("VpcId", ""),
                    subnet_id=raw.get("SubnetId", ""),
                    state=raw.get("State", "unknown"),
                    bytes_processed=processed,
                )
            )
    logger.info("collected %d NAT gateways", len(gateways))
    return gateways


# ---------------------------------------------------------------------------
# ELBv2
# ---------------------------------------------------------------------------


def collect_load_balancers(elbv2: Any) -> list[LoadBalancer]:
    """ELBv2 load balancers with per-target-group health counts."""
    groups_by_lb: dict[str, list[dict[str, Any]]] = {}
    for page in elbv2.get_paginator("describe_target_groups").paginate():
        for group in page.get("TargetGroups", []):
            for lb_arn in group.get("LoadBalancerArns", []):
                groups_by_lb.setdefault(lb_arn, []).append(group)

    balancers: list[LoadBalancer] = []
    for page in elbv2.get_paginator("describe_load_balancers").paginate():
        for raw in page.get("LoadBalancers", []):
            arn = raw["LoadBalancerArn"]
            healths: list[TargetGroupHealth] = []
            for group in groups_by_lb.get(arn, []):
                response = elbv2.describe_target_health(TargetGroupArn=group["TargetGroupArn"])
                descriptions = response.get("TargetHealthDescriptions", [])
                healthy = sum(
                    1
                    for item in descriptions
                    if item.get("TargetHealth", {}).get("State") == "healthy"
                )
                healths.append(
                    TargetGroupHealth(
                        target_group_arn=group["TargetGroupArn"],
                        name=group.get("TargetGroupName", ""),
                        registered_targets=len(descriptions),
                        healthy_targets=healthy,
                    )
                )
            balancers.append(
                LoadBalancer(
                    arn=arn,
                    name=raw.get("LoadBalancerName", ""),
                    lb_type=raw.get("Type", "application"),
                    scheme=raw.get("Scheme", ""),
                    created_time=_as_utc(raw.get("CreatedTime")),
                    target_groups=tuple(healths),
                )
            )
    logger.info("collected %d load balancers", len(balancers))
    return balancers


# ---------------------------------------------------------------------------
# RDS
# ---------------------------------------------------------------------------


def collect_db_instances(rds: Any, metrics: MetricReader) -> list[DbInstance]:
    """RDS instances with average connections and CPU over the lookback window."""
    databases: list[DbInstance] = []
    for page in rds.get_paginator("describe_db_instances").paginate():
        for raw in page.get("DBInstances", []):
            db_id = raw["DBInstanceIdentifier"]
            status = raw.get("DBInstanceStatus", "unknown")
            connections = cpu = None
            if status == "available":
                connections = metrics.statistic(
                    "AWS/RDS", "DatabaseConnections", {"DBInstanceIdentifier": db_id}
                )
                cpu = metrics.statistic(
                    "AWS/RDS", "CPUUtilization", {"DBInstanceIdentifier": db_id}
                )
            databases.append(
                DbInstance(
                    db_instance_id=db_id,
                    instance_class=raw.get("DBInstanceClass", "unknown"),
                    engine=raw.get("Engine", "unknown"),
                    status=status,
                    multi_az=bool(raw.get("MultiAZ", False)),
                    allocated_storage_gib=int(raw.get("AllocatedStorage", 0)),
                    storage_type=raw.get("StorageType", "gp3"),
                    avg_connections=connections,
                    avg_cpu_percent=cpu,
                    create_time=_as_utc(raw["InstanceCreateTime"])
                    if raw.get("InstanceCreateTime")
                    else None,
                )
            )
    logger.info("collected %d RDS instances", len(databases))
    return databases


# ---------------------------------------------------------------------------
# S3
# ---------------------------------------------------------------------------


def _bucket_region(s3: Any, name: str) -> str:
    try:
        location = s3.get_bucket_location(Bucket=name).get("LocationConstraint")
    except ClientError as exc:
        logger.debug("get_bucket_location(%s) failed: %s", name, exc)
        return "unknown"
    if not location:
        return "us-east-1"
    if location == "EU":
        return "eu-west-1"
    return str(location)


def _has_lifecycle_rules(s3: Any, name: str) -> bool:
    try:
        response = s3.get_bucket_lifecycle_configuration(Bucket=name)
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "")
        if code == "NoSuchLifecycleConfiguration":
            return False
        logger.warning("cannot read lifecycle configuration of %s: %s", name, exc)
        return True  # unknown: do not accuse the bucket
    return any(rule.get("Status") == "Enabled" for rule in response.get("Rules", []))


def collect_buckets(s3: Any, metrics: MetricReader, region: str) -> list[Bucket]:
    """All buckets; storage size is only looked up for buckets in the scan region."""
    buckets: list[Bucket] = []
    for raw in s3.list_buckets().get("Buckets", []):
        name = raw["Name"]
        bucket_region = _bucket_region(s3, name)
        size_gb: float | None = None
        if bucket_region == region:
            size_bytes = metrics.statistic(
                "AWS/S3",
                "BucketSizeBytes",
                {"BucketName": name, "StorageType": "StandardStorage"},
                latest=True,
            )
            if size_bytes is not None:
                size_gb = size_bytes / BYTES_PER_GIB
        buckets.append(
            Bucket(
                name=name,
                region=bucket_region,
                has_lifecycle_rules=_has_lifecycle_rules(s3, name),
                size_gb=size_gb,
            )
        )
    logger.info("collected %d S3 buckets", len(buckets))
    return buckets


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def _safely(inventory: Inventory, label: str, action: Callable[[], list[Any]]) -> list[Any]:
    """Run one collector; record success or a per-service error on ``inventory``.

    Credential problems are re-raised as :class:`CollectionError` because every
    following call would fail the same way.
    """
    try:
        items = action()
    except (ClientError, BotoCoreError) as exc:
        if is_credential_error(exc):
            raise CollectionError(
                f"AWS credentials are missing or invalid ({label}: {exc}); configure a "
                "profile, environment variables or an assumed role and run the scan again"
            ) from exc
        message = f"{label}: {exc}"
        logger.warning("skipping %s", message)
        inventory.errors.append(message)
        return []
    inventory.collected.append(label)
    return items


def collect_inventory(
    clients: AwsClients,
    config: ScanConfig,
    *,
    include_cost_report: bool = True,
) -> Inventory:
    """Collect every resource type plus (optionally) the Cost Explorer report."""
    now = config.now
    metrics = MetricReader(clients.cloudwatch, end=now, lookback_days=config.lookback_days)
    inventory = Inventory(region=clients.region, collected_at=now)

    inventory.instances = _safely(
        inventory, "ec2:DescribeInstances", lambda: collect_instances(clients.ec2, metrics)
    )
    instance_states = {item.instance_id: item.state for item in inventory.instances}
    inventory.volumes = _safely(
        inventory, "ec2:DescribeVolumes", lambda: collect_volumes(clients.ec2)
    )
    inventory.snapshots = _safely(
        inventory, "ec2:DescribeSnapshots", lambda: collect_snapshots(clients.ec2)
    )
    inventory.elastic_ips = _safely(
        inventory,
        "ec2:DescribeAddresses",
        lambda: collect_elastic_ips(clients.ec2, instance_states),
    )
    inventory.nat_gateways = _safely(
        inventory, "ec2:DescribeNatGateways", lambda: collect_nat_gateways(clients.ec2, metrics)
    )
    inventory.load_balancers = _safely(
        inventory,
        "elasticloadbalancing:DescribeLoadBalancers",
        lambda: collect_load_balancers(clients.elbv2),
    )
    inventory.db_instances = _safely(
        inventory, "rds:DescribeDBInstances", lambda: collect_db_instances(clients.rds, metrics)
    )
    inventory.buckets = _safely(
        inventory,
        "s3:ListAllMyBuckets",
        lambda: collect_buckets(clients.s3, metrics, clients.region),
    )

    if not inventory.collected:
        raise CollectionError(
            f"no resource could be collected in {clients.region}; every service failed: "
            + "; ".join(inventory.errors)
        )

    if include_cost_report:
        try:
            inventory.cost_report = fetch_cost_report(clients.ce, config.months, today=now.date())
            inventory.collected.append("ce:GetCostAndUsage")
        except CostExplorerError as exc:
            cause = exc.__cause__
            if isinstance(cause, Exception) and is_credential_error(cause):
                raise CollectionError(f"AWS credentials rejected by Cost Explorer: {exc}") from exc
            logger.warning("cost explorer unavailable: %s", exc)
            inventory.errors.append(f"ce:GetCostAndUsage: {exc}")
    attempted = len(inventory.collected) + len(inventory.errors)
    logger.info("collected %d of %d services", len(inventory.collected), attempted)
    return inventory
