"""Every inventory check, driven end to end through the boto3 collectors on moto."""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from cost_sentinel.aws.collectors import collect_inventory
from cost_sentinel.aws.session import AwsClients
from cost_sentinel.checks import (
    Gp2ToGp3Check,
    IdleEc2Check,
    IdleLoadBalancerCheck,
    IdleRdsCheck,
    NatGatewayCheck,
    OldSnapshotCheck,
    S3LifecycleCheck,
    SavingsPlanCheck,
    UnattachedEbsCheck,
    UnusedElasticIpCheck,
)
from cost_sentinel.checks.base import BaseCheck
from cost_sentinel.models import Confidence, Finding, ScanConfig
from tests.conftest import (
    REGION,
    FakeCloudWatch,
    config_at,
    default_subnet,
    put_metric,
    run_instance,
)

GIB = 1024**3


def _run(check: BaseCheck, aws: AwsClients, config: ScanConfig) -> list[Finding]:
    inventory = collect_inventory(aws, config, include_cost_report=False)
    assert inventory.errors == []
    return check.run(inventory, config)


# --- EC2 ---------------------------------------------------------------------------


def test_idle_ec2_flags_low_cpu_and_ignores_busy_instances(aws: AwsClients, now: datetime) -> None:
    idle = run_instance(aws, "m5.large")
    busy = run_instance(aws, "m5.large")
    silent = run_instance(aws, "m5.large")  # no CloudWatch data at all
    put_metric(aws, "AWS/EC2", "CPUUtilization", {"InstanceId": idle}, 1.2)
    put_metric(aws, "AWS/EC2", "CPUUtilization", {"InstanceId": busy}, 55.0)

    findings = _run(IdleEc2Check(), aws, config_at(now))

    assert [f.resource_id for f in findings] == [idle]
    finding = findings[0]
    assert finding.estimated_monthly_savings == pytest.approx(0.096 * 730)
    # Launched minutes ago, so shorter than the lookback window: low confidence.
    assert finding.confidence is Confidence.LOW
    assert finding.details["avg_cpu_percent"] == pytest.approx(1.2)
    assert silent not in {f.resource_id for f in findings}


def test_idle_ec2_respects_a_custom_threshold(aws: AwsClients, now: datetime) -> None:
    instance = run_instance(aws, "t3.medium")
    put_metric(aws, "AWS/EC2", "CPUUtilization", {"InstanceId": instance}, 8.0)
    assert _run(IdleEc2Check(), aws, config_at(now)) == []
    findings = _run(IdleEc2Check(), aws, config_at(now, ec2_cpu_threshold=10.0))
    assert len(findings) == 1


def test_stopped_instances_are_not_idle_findings(aws: AwsClients, now: datetime) -> None:
    instance = run_instance(aws)
    put_metric(aws, "AWS/EC2", "CPUUtilization", {"InstanceId": instance}, 0.5)
    aws.ec2.stop_instances(InstanceIds=[instance])
    assert _run(IdleEc2Check(), aws, config_at(now)) == []


def test_savings_plan_covers_long_running_on_demand_instances(
    aws: AwsClients, now: datetime
) -> None:
    run_instance(aws, "m5.large")
    run_instance(aws, "c5.large")

    assert _run(SavingsPlanCheck(), aws, config_at(now)) == []  # too young

    findings = _run(SavingsPlanCheck(), aws, config_at(now, days_ahead=45))
    assert len(findings) == 1
    finding = findings[0]
    on_demand_monthly = (0.096 + 0.085) * 730
    assert finding.monthly_cost == pytest.approx(on_demand_monthly)
    assert finding.estimated_monthly_savings == pytest.approx(on_demand_monthly * 0.20)
    assert finding.details["commitment_hourly"] == pytest.approx((0.096 + 0.085) * 0.8, abs=1e-4)
    assert finding.confidence is Confidence.MEDIUM


# --- EBS -----------------------------------------------------------------------------


def test_unattached_ebs_volume_is_flagged(aws: AwsClients, now: datetime) -> None:
    zone = f"{REGION}a"
    loose = aws.ec2.create_volume(AvailabilityZone=zone, Size=100, VolumeType="gp3")["VolumeId"]
    attached = aws.ec2.create_volume(AvailabilityZone=zone, Size=50, VolumeType="gp3")["VolumeId"]
    instance = run_instance(aws)
    aws.ec2.attach_volume(VolumeId=attached, InstanceId=instance, Device="/dev/sdf")

    fresh = _run(UnattachedEbsCheck(), aws, config_at(now))
    assert [f.resource_id for f in fresh] == [loose]
    assert fresh[0].confidence is Confidence.MEDIUM
    assert fresh[0].estimated_monthly_savings == pytest.approx(8.0)

    aged = _run(UnattachedEbsCheck(), aws, config_at(now, days_ahead=10))
    assert aged[0].confidence is Confidence.HIGH


def test_gp2_volumes_convertible_to_gp3(aws: AwsClients, now: datetime) -> None:
    zone = f"{REGION}a"
    small = aws.ec2.create_volume(AvailabilityZone=zone, Size=500, VolumeType="gp2")["VolumeId"]
    large = aws.ec2.create_volume(AvailabilityZone=zone, Size=2000, VolumeType="gp2")["VolumeId"]
    loose = aws.ec2.create_volume(AvailabilityZone=zone, Size=300, VolumeType="gp2")["VolumeId"]
    aws.ec2.create_volume(AvailabilityZone=zone, Size=500, VolumeType="gp3")
    instance = run_instance(aws)
    aws.ec2.attach_volume(VolumeId=small, InstanceId=instance, Device="/dev/sdf")
    aws.ec2.attach_volume(VolumeId=large, InstanceId=instance, Device="/dev/sdg")

    findings = {f.resource_id: f for f in _run(Gp2ToGp3Check(), aws, config_at(now))}

    # The instance's 8 GiB gp2 root volume is attached too; the unattached gp2 volume is
    # left to unattached-ebs, because deleting it saves more than converting it.
    assert {small, large} <= set(findings)
    assert loose not in findings
    assert findings[small].estimated_monthly_savings == pytest.approx(500 * 0.02)
    assert findings[small].confidence is Confidence.HIGH
    # 2000 GiB gp2 has a 6000 IOPS baseline: gp3 needs 3000 extra provisioned IOPS.
    assert findings[large].estimated_monthly_savings == pytest.approx(200 - (160 + 15))
    assert findings[large].confidence is Confidence.MEDIUM
    assert findings[large].details["required_iops"] == 6000


def test_old_snapshots_are_flagged_after_max_age(aws: AwsClients, now: datetime) -> None:
    volume = aws.ec2.create_volume(AvailabilityZone=f"{REGION}a", Size=200)["VolumeId"]
    snapshot = aws.ec2.create_snapshot(VolumeId=volume)["SnapshotId"]

    assert _run(OldSnapshotCheck(), aws, config_at(now, days_ahead=30)) == []

    findings = _run(OldSnapshotCheck(), aws, config_at(now, days_ahead=120))
    assert [f.resource_id for f in findings] == [snapshot]
    assert findings[0].estimated_monthly_savings == pytest.approx(200 * 0.05 * 0.5)
    assert findings[0].confidence is Confidence.LOW  # source volume still exists

    aws.ec2.delete_volume(VolumeId=volume)
    orphaned = _run(OldSnapshotCheck(), aws, config_at(now, days_ahead=120))
    assert orphaned[0].confidence is Confidence.MEDIUM
    assert "no longer exists" in orphaned[0].reason


def test_snapshots_backing_an_ami_are_skipped(aws: AwsClients, now: datetime) -> None:
    volume = aws.ec2.create_volume(AvailabilityZone=f"{REGION}a", Size=8)["VolumeId"]
    loose = aws.ec2.create_snapshot(VolumeId=volume)["SnapshotId"]
    image_id = aws.ec2.register_image(Name="golden-image", RootDeviceName="/dev/xvda")["ImageId"]
    image = aws.ec2.describe_images(ImageIds=[image_id])["Images"][0]
    backing = image["BlockDeviceMappings"][0]["Ebs"]["SnapshotId"]

    findings = {
        f.resource_id for f in _run(OldSnapshotCheck(), aws, config_at(now, days_ahead=365))
    }

    assert loose in findings
    assert backing not in findings


# --- Network -------------------------------------------------------------------------


def test_unused_elastic_ips(aws: AwsClients, now: datetime) -> None:
    loose = aws.ec2.allocate_address(Domain="vpc")["AllocationId"]
    on_running = aws.ec2.allocate_address(Domain="vpc")["AllocationId"]
    on_stopped = aws.ec2.allocate_address(Domain="vpc")["AllocationId"]
    running = run_instance(aws)
    stopped = run_instance(aws)
    aws.ec2.associate_address(AllocationId=on_running, InstanceId=running)
    aws.ec2.associate_address(AllocationId=on_stopped, InstanceId=stopped)
    aws.ec2.stop_instances(InstanceIds=[stopped])

    findings = {f.resource_id: f for f in _run(UnusedElasticIpCheck(), aws, config_at(now))}

    assert set(findings) == {loose, on_stopped}
    assert findings[loose].confidence is Confidence.HIGH
    assert findings[on_stopped].confidence is Confidence.MEDIUM
    assert findings[loose].estimated_monthly_savings == pytest.approx(3.65)


def _load_balancer(aws: AwsClients, name: str) -> tuple[str, str]:
    vpc_id, _ = default_subnet(aws)
    subnets = [
        subnet["SubnetId"]
        for subnet in aws.ec2.describe_subnets(Filters=[{"Name": "vpc-id", "Values": [vpc_id]}])[
            "Subnets"
        ]
    ][:2]
    lb_arn = aws.elbv2.create_load_balancer(Name=name, Subnets=subnets)["LoadBalancers"][0][
        "LoadBalancerArn"
    ]
    tg_arn = aws.elbv2.create_target_group(
        Name=f"{name}-tg", Protocol="HTTP", Port=80, VpcId=vpc_id, TargetType="instance"
    )["TargetGroups"][0]["TargetGroupArn"]
    aws.elbv2.create_listener(
        LoadBalancerArn=lb_arn,
        Protocol="HTTP",
        Port=80,
        DefaultActions=[{"Type": "forward", "TargetGroupArn": tg_arn}],
    )
    return lb_arn, tg_arn


def test_load_balancer_without_targets_is_flagged(aws: AwsClients, now: datetime) -> None:
    _load_balancer(aws, "orphan-alb")
    _, serving_tg = _load_balancer(aws, "serving-alb")
    aws.elbv2.register_targets(TargetGroupArn=serving_tg, Targets=[{"Id": run_instance(aws)}])

    findings = _run(IdleLoadBalancerCheck(), aws, config_at(now, days_ahead=30))

    assert [f.resource_id for f in findings] == ["orphan-alb"]
    assert findings[0].confidence is Confidence.HIGH
    assert findings[0].estimated_monthly_savings == pytest.approx(0.0225 * 730)
    assert findings[0].details["target_groups"] == ["orphan-alb-tg"]


def _nat_gateway(aws: AwsClients) -> str:
    _, subnet_id = default_subnet(aws)
    allocation = aws.ec2.allocate_address(Domain="vpc")["AllocationId"]
    return aws.ec2.create_nat_gateway(SubnetId=subnet_id, AllocationId=allocation)["NatGateway"][
        "NatGatewayId"
    ]


def test_nat_gateway_without_traffic_metrics(aws: AwsClients, now: datetime) -> None:
    nat_id = _nat_gateway(aws)
    findings = _run(NatGatewayCheck(), aws, config_at(now))
    assert [f.resource_id for f in findings] == [nat_id]
    assert findings[0].confidence is Confidence.MEDIUM
    assert findings[0].estimated_monthly_savings == pytest.approx(32.85)


def test_nat_gateway_idle_and_busy_traffic(aws: AwsClients, now: datetime) -> None:
    idle = _nat_gateway(aws)
    busy = _nat_gateway(aws)
    put_metric(aws, "AWS/NATGateway", "BytesOutToDestination", {"NatGatewayId": idle}, 2e8)
    # 14 days * ~1.4 TB scales to 3000 GB/month: processing dwarfs the hourly fee.
    put_metric(aws, "AWS/NATGateway", "BytesOutToDestination", {"NatGatewayId": busy}, 1.4e12)

    findings = {f.resource_id: f for f in _run(NatGatewayCheck(), aws, config_at(now))}

    assert findings[idle].confidence is Confidence.HIGH
    assert findings[idle].details["gb_processed_in_window"] == pytest.approx(0.2)
    processing_monthly = 1400 * 30 / 14 * 0.045
    assert findings[busy].confidence is Confidence.LOW
    assert findings[busy].estimated_monthly_savings == pytest.approx(processing_monthly * 0.3)
    assert findings[busy].monthly_cost == pytest.approx(32.85 + processing_monthly)


# --- RDS -----------------------------------------------------------------------------


def _database(aws: AwsClients, identifier: str, multi_az: bool = False) -> None:
    aws.rds.create_db_instance(
        DBInstanceIdentifier=identifier,
        DBInstanceClass="db.m5.large",
        Engine="postgres",
        AllocatedStorage=100,
        StorageType="gp3",
        MasterUsername="admin",
        MasterUserPassword="not-a-real-password",
        MultiAZ=multi_az,
    )


def test_idle_rds_instance(aws: AwsClients, now: datetime) -> None:
    _database(aws, "orders-staging", multi_az=True)
    _database(aws, "orders-prod")
    _database(aws, "no-metrics")
    dims_idle = {"DBInstanceIdentifier": "orders-staging"}
    dims_busy = {"DBInstanceIdentifier": "orders-prod"}
    put_metric(aws, "AWS/RDS", "DatabaseConnections", dims_idle, 0.0)
    put_metric(aws, "AWS/RDS", "CPUUtilization", dims_idle, 2.0)
    put_metric(aws, "AWS/RDS", "DatabaseConnections", dims_busy, 42.0)
    put_metric(aws, "AWS/RDS", "CPUUtilization", dims_busy, 30.0)

    findings = _run(IdleRdsCheck(), aws, config_at(now))

    assert [f.resource_id for f in findings] == ["orders-staging"]
    finding = findings[0]
    # Created minutes ago, so younger than the lookback window: not high confidence.
    assert finding.confidence is Confidence.MEDIUM
    assert finding.estimated_monthly_savings == pytest.approx(0.171 * 2 * 730)
    assert finding.monthly_cost == pytest.approx(0.171 * 2 * 730 + 100 * 0.115)
    assert finding.details["multi_az"] is True


def test_rds_with_few_connections_but_busy_cpu_is_not_idle(aws: AwsClients, now: datetime) -> None:
    _database(aws, "batch-db")
    dims = {"DBInstanceIdentifier": "batch-db"}
    put_metric(aws, "AWS/RDS", "DatabaseConnections", dims, 0.5)
    put_metric(aws, "AWS/RDS", "CPUUtilization", dims, 70.0)
    assert _run(IdleRdsCheck(), aws, config_at(now)) == []


# --- S3 ------------------------------------------------------------------------------


def test_s3_buckets_without_lifecycle_rules(aws: AwsClients, now: datetime) -> None:
    for name in ("raw-logs", "managed-logs", "tiny-bucket", "unmeasured"):
        aws.s3.create_bucket(Bucket=name)
    aws.s3.put_bucket_lifecycle_configuration(
        Bucket="managed-logs",
        LifecycleConfiguration={
            "Rules": [
                {
                    "ID": "expire",
                    "Status": "Enabled",
                    "Filter": {"Prefix": ""},
                    "Expiration": {"Days": 30},
                }
            ]
        },
    )
    metrics = FakeCloudWatch()
    storage = {"StorageType": "StandardStorage"}
    metrics.put("AWS/S3", "BucketSizeBytes", {"BucketName": "raw-logs", **storage}, 500 * GIB)
    metrics.put("AWS/S3", "BucketSizeBytes", {"BucketName": "tiny-bucket", **storage}, 1024)
    aws.cloudwatch = metrics

    findings = {f.resource_id: f for f in _run(S3LifecycleCheck(), aws, config_at(now))}

    assert set(findings) == {"raw-logs", "unmeasured"}
    assert findings["raw-logs"].estimated_monthly_savings == pytest.approx(
        500 * (0.023 - 0.0125) * 0.3
    )
    assert findings["unmeasured"].estimated_monthly_savings == 0.0
    assert all(f.confidence is Confidence.LOW for f in findings.values())


def test_full_inventory_counts_every_resource_type(aws: AwsClients, now: datetime) -> None:
    run_instance(aws)
    aws.ec2.allocate_address(Domain="vpc")
    aws.s3.create_bucket(Bucket="inventory-bucket")
    _database(aws, "inventory-db")
    inventory = collect_inventory(aws, config_at(now), include_cost_report=False)
    counts = inventory.counts()
    assert counts["instances"] == 1
    assert counts["elastic_ips"] == 1
    assert counts["buckets"] == 1
    assert counts["db_instances"] == 1
    assert inventory.collected_at <= now + timedelta(minutes=10)
