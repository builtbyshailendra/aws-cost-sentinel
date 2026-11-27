"""Run a full live-mode scan against a seeded, in-memory AWS account (moto).

No credentials and no network are needed; the script needs the ``dev`` extra
(``uv pip install -e ".[dev]"``). It creates one example of every kind of waste the
checks look for, collects the inventory with the real collectors, back-dates the
collected creation times by 120 days (moto stamps every resource "now", so nothing
would look old otherwise) and prints the report.

    python scripts/demo_moto_scan.py --format markdown
"""

from __future__ import annotations

import argparse
import dataclasses
import os
import sys
from datetime import UTC, datetime, timedelta

# Fake credentials only, and no local profiles: even if moto were bypassed, nothing
# here could authenticate against a real AWS account.
os.environ["AWS_ACCESS_KEY_ID"] = "demo"
os.environ["AWS_SECRET_ACCESS_KEY"] = "demo"
os.environ.pop("AWS_SESSION_TOKEN", None)
os.environ.pop("AWS_SECURITY_TOKEN", None)
os.environ.pop("AWS_PROFILE", None)
os.environ["AWS_CONFIG_FILE"] = os.devnull
os.environ["AWS_SHARED_CREDENTIALS_FILE"] = os.devnull
os.environ["AWS_DEFAULT_REGION"] = "us-east-1"
os.environ["MOTO_EC2_LOAD_DEFAULT_AMIS"] = "false"

# Imported after the environment is prepared so boto3 and moto pick it up.
import boto3
from moto import mock_aws

from cost_sentinel.aws.collectors import collect_inventory
from cost_sentinel.aws.session import AwsClients
from cost_sentinel.models import Inventory, ScanConfig
from cost_sentinel.report import render
from cost_sentinel.scanner import run_scan

REGION = "us-east-1"
BACKDATE_DAYS = 120


def _metric(
    clients: AwsClients,
    namespace: str,
    name: str,
    dims: dict[str, str],
    value: float,
    *,
    at: datetime,
) -> None:
    clients.cloudwatch.put_metric_data(
        Namespace=namespace,
        MetricData=[
            {
                "MetricName": name,
                "Dimensions": [{"Name": k, "Value": v} for k, v in dims.items()],
                "Timestamp": at,
                "Value": value,
            }
        ],
    )


def seed(clients: AwsClients, metrics_at: datetime) -> None:
    """Create one example of each kind of waste."""
    ec2, elbv2, rds, s3 = clients.ec2, clients.elbv2, clients.rds, clients.s3
    image = ec2.register_image(Name="demo-base", RootDeviceName="/dev/xvda")["ImageId"]

    def launch(instance_type: str, name: str) -> str:
        return ec2.run_instances(
            ImageId=image,
            InstanceType=instance_type,
            MinCount=1,
            MaxCount=1,
            TagSpecifications=[
                {"ResourceType": "instance", "Tags": [{"Key": "Name", "Value": name}]}
            ],
        )["Instances"][0]["InstanceId"]

    idle = launch("m5.xlarge", "legacy-reporting")
    busy = launch("c5.large", "api-worker")
    _metric(clients, "AWS/EC2", "CPUUtilization", {"InstanceId": idle}, 1.3, at=metrics_at)
    _metric(clients, "AWS/EC2", "CPUUtilization", {"InstanceId": busy}, 46.0, at=metrics_at)

    zone = f"{REGION}a"
    ec2.create_volume(AvailabilityZone=zone, Size=500, VolumeType="gp3")
    data = ec2.create_volume(AvailabilityZone=zone, Size=1500, VolumeType="gp2")["VolumeId"]
    ec2.attach_volume(VolumeId=data, InstanceId=busy, Device="/dev/sdf")
    scratch = ec2.create_volume(AvailabilityZone=zone, Size=200, VolumeType="gp2")["VolumeId"]
    ec2.create_snapshot(VolumeId=scratch, Description="pre-migration backup")
    ec2.delete_volume(VolumeId=scratch)

    ec2.allocate_address(Domain="vpc")

    vpc = ec2.describe_vpcs()["Vpcs"][0]["VpcId"]
    subnets = [
        subnet["SubnetId"]
        for subnet in ec2.describe_subnets(Filters=[{"Name": "vpc-id", "Values": [vpc]}])["Subnets"]
    ]
    lb = elbv2.create_load_balancer(Name="old-campaign-alb", Subnets=subnets[:2])
    group = elbv2.create_target_group(Name="old-campaign", Protocol="HTTP", Port=80, VpcId=vpc)
    elbv2.create_listener(
        LoadBalancerArn=lb["LoadBalancers"][0]["LoadBalancerArn"],
        Protocol="HTTP",
        Port=80,
        DefaultActions=[
            {"Type": "forward", "TargetGroupArn": group["TargetGroups"][0]["TargetGroupArn"]}
        ],
    )

    nat_eip = ec2.allocate_address(Domain="vpc")["AllocationId"]
    nat = ec2.create_nat_gateway(SubnetId=subnets[0], AllocationId=nat_eip)["NatGateway"]
    _metric(
        clients,
        "AWS/NATGateway",
        "BytesOutToDestination",
        {"NatGatewayId": nat["NatGatewayId"]},
        2.1e12,
        at=metrics_at,
    )

    rds.create_db_instance(
        DBInstanceIdentifier="orders-staging",
        DBInstanceClass="db.r6g.large",
        Engine="postgres",
        AllocatedStorage=200,
        MasterUsername="demo",
        MasterUserPassword="demo-password-1",
        MultiAZ=True,
    )
    dims = {"DBInstanceIdentifier": "orders-staging"}
    _metric(clients, "AWS/RDS", "DatabaseConnections", dims, 0.0, at=metrics_at)
    _metric(clients, "AWS/RDS", "CPUUtilization", dims, 1.8, at=metrics_at)

    s3.create_bucket(Bucket="demo-app-logs")
    _metric(
        clients,
        "AWS/S3",
        "BucketSizeBytes",
        {"BucketName": "demo-app-logs", "StorageType": "StandardStorage"},
        800 * 1024**3,
        at=metrics_at,
    )


def backdate(inventory: Inventory, days: float) -> Inventory:
    """Move every creation timestamp in ``inventory`` ``days`` into the past."""
    shift = timedelta(days=days)
    inventory.instances = [
        dataclasses.replace(item, launch_time=item.launch_time - shift)
        for item in inventory.instances
    ]
    inventory.volumes = [
        dataclasses.replace(item, create_time=item.create_time - shift)
        for item in inventory.volumes
    ]
    inventory.snapshots = [
        dataclasses.replace(item, start_time=item.start_time - shift)
        for item in inventory.snapshots
    ]
    inventory.load_balancers = [
        dataclasses.replace(item, created_time=item.created_time - shift)
        for item in inventory.load_balancers
    ]
    inventory.db_instances = [
        dataclasses.replace(
            item, create_time=item.create_time - shift if item.create_time else None
        )
        for item in inventory.db_instances
    ]
    return inventory


def main(argv: list[str] | None = None) -> int:
    """Seed moto, scan it and print the report."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--format", default="markdown", choices=("markdown", "html", "json"))
    args = parser.parse_args(argv)

    scan_time = datetime.now(UTC)
    with mock_aws():
        clients = AwsClients.from_session(boto3.Session(region_name=REGION), REGION)
        seed(clients, metrics_at=scan_time - timedelta(hours=2))
        config = ScanConfig(region=REGION, reference_time=scan_time + timedelta(minutes=1))
        inventory = backdate(
            collect_inventory(clients, config, include_cost_report=False), BACKDATE_DAYS
        )
        result = run_scan(
            inventory,
            config,
            source="live",
            source_detail=f"moto demo account, resources back-dated {BACKDATE_DAYS} days",
        )
    sys.stdout.write(render(result, args.format))
    return 0


if __name__ == "__main__":
    sys.exit(main())
