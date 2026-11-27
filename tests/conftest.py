"""Shared fixtures: fake AWS credentials, moto-backed clients and inventories."""

from __future__ import annotations

import dataclasses
import os
import socket
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import boto3
import pytest
from moto import mock_aws

from cost_sentinel.aws.session import AwsClients
from cost_sentinel.models import ScanConfig

# moto ships ~1,300 public AMIs (each with a snapshot) unless told not to; they would
# drown the snapshot tests and slow every EC2 test down.
os.environ["MOTO_EC2_LOAD_DEFAULT_AMIS"] = "false"

REGION = "us-east-1"
ROOT = Path(__file__).resolve().parent.parent
EXAMPLES = ROOT / "examples"
BY_SERVICE_CSV = EXAMPLES / "cost_explorer_by_service.csv"
BY_MONTH_CSV = EXAMPLES / "cost_explorer_by_month.csv"

_LOCAL_HOSTS = {None, "localhost", "127.0.0.1", "::1"}
_real_connect = socket.socket.connect
_real_getaddrinfo = socket.getaddrinfo


def _guarded_connect(self: socket.socket, address: Any) -> None:
    host = address[0] if isinstance(address, tuple) else address
    if host not in _LOCAL_HOSTS:
        raise RuntimeError(f"test tried to open a network connection to {address!r}")
    _real_connect(self, address)


def _guarded_getaddrinfo(host: Any, *args: Any, **kwargs: Any) -> Any:
    if host not in _LOCAL_HOSTS:
        raise RuntimeError(f"test tried to resolve {host!r}")
    return _real_getaddrinfo(host, *args, **kwargs)


@pytest.fixture(autouse=True)
def no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail any test that tries to leave the machine; moto and stubs never need to."""
    monkeypatch.setattr(socket.socket, "connect", _guarded_connect)
    monkeypatch.setattr(socket, "getaddrinfo", _guarded_getaddrinfo)


@pytest.fixture(autouse=True)
def fake_aws_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make sure nothing can ever reach a real AWS account or read local profiles."""
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_SECURITY_TOKEN", "testing")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", REGION)
    monkeypatch.delenv("AWS_PROFILE", raising=False)
    monkeypatch.setenv("MOTO_EC2_LOAD_DEFAULT_AMIS", "false")
    monkeypatch.setenv("AWS_CONFIG_FILE", str(ROOT / "tests" / "missing-aws-config"))
    monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", str(ROOT / "tests" / "missing-aws-creds"))
    for name in (
        "COST_SENTINEL_LOOKBACK_DAYS",
        "COST_SENTINEL_CPU_THRESHOLD",
        "COST_SENTINEL_SNAPSHOT_AGE_DAYS",
        "COST_SENTINEL_MONTHS",
        "COST_SENTINEL_FORMAT",
        "GITHUB_TOKEN",
        "GITHUB_REPOSITORY",
        "GITHUB_EVENT_PATH",
    ):
        monkeypatch.delenv(name, raising=False)


_CLIENTS: list[AwsClients] = []


def _cached_clients() -> AwsClients:
    """Create the boto3 clients once per test session.

    Building a client loads and parses its service model, which costs far more than
    the moto calls in a typical test. moto intercepts requests at send time and
    resets its in-memory state on every ``mock_aws()``, so reusing the client objects
    does not leak resources between tests. Each test gets a fresh ``AwsClients``
    wrapper, so tests can still replace a client on it.
    """
    if not _CLIENTS:
        _CLIENTS.append(AwsClients.from_session(boto3.Session(region_name=REGION), REGION))
    return dataclasses.replace(_CLIENTS[0])


@pytest.fixture
def aws() -> Iterator[AwsClients]:
    """All clients a scan uses, backed by moto's in-memory AWS."""
    with mock_aws():
        yield _cached_clients()


@pytest.fixture
def now() -> datetime:
    """Real wall-clock time; moto stamps resources with it."""
    return datetime.now(UTC)


def config_at(now: datetime, *, days_ahead: float = 0.0, **overrides: object) -> ScanConfig:
    """A config whose reference time is ``days_ahead`` days after ``now``."""
    return ScanConfig(
        region=REGION,
        reference_time=now + timedelta(days=days_ahead, minutes=5),
        **overrides,  # type: ignore[arg-type]
    )


def put_metric(
    aws: AwsClients,
    namespace: str,
    name: str,
    dimensions: dict[str, str],
    value: float,
    *,
    when: datetime | None = None,
) -> None:
    """Store one CloudWatch datapoint in moto (timestamped now unless ``when`` is given)."""
    aws.cloudwatch.put_metric_data(
        Namespace=namespace,
        MetricData=[
            {
                "MetricName": name,
                "Dimensions": [{"Name": key, "Value": val} for key, val in dimensions.items()],
                "Timestamp": when or datetime.now(UTC),
                "Value": value,
            }
        ],
    )


def default_subnet(aws: AwsClients) -> tuple[str, str]:
    """Return ``(vpc_id, subnet_id)`` of moto's default VPC."""
    vpc_id = aws.ec2.describe_vpcs(Filters=[{"Name": "isDefault", "Values": ["true"]}])["Vpcs"][0][
        "VpcId"
    ]
    subnets = aws.ec2.describe_subnets(Filters=[{"Name": "vpc-id", "Values": [vpc_id]}])["Subnets"]
    return vpc_id, subnets[0]["SubnetId"]


def base_image(aws: AwsClients) -> str:
    """Id of a self-owned AMI to launch instances from (registered on first use)."""
    images = aws.ec2.describe_images(
        Owners=["self"], Filters=[{"Name": "name", "Values": ["base-image"]}]
    )["Images"]
    if images:
        return images[0]["ImageId"]
    return aws.ec2.register_image(Name="base-image", RootDeviceName="/dev/xvda")["ImageId"]


def run_instance(aws: AwsClients, instance_type: str = "m5.large") -> str:
    """Launch one instance and return its id."""
    response = aws.ec2.run_instances(
        ImageId=base_image(aws), InstanceType=instance_type, MinCount=1, MaxCount=1
    )
    return response["Instances"][0]["InstanceId"]


class FakeCloudWatch:
    """Minimal GetMetricStatistics stand-in with exact dimension matching.

    moto's CloudWatch also serves the S3 storage metrics it derives from the objects
    actually stored, which cannot represent a 500 GiB bucket in a unit test.
    """

    def __init__(self) -> None:
        self._points: dict[tuple[str, str, frozenset[tuple[str, str]]], list[float]] = {}

    def put(self, namespace: str, name: str, dimensions: dict[str, str], value: float) -> None:
        key = (namespace, name, frozenset(dimensions.items()))
        self._points.setdefault(key, []).append(value)

    def get_metric_statistics(self, **kwargs: Any) -> dict[str, Any]:
        dimensions = frozenset((d["Name"], d["Value"]) for d in kwargs["Dimensions"])
        values = self._points.get((kwargs["Namespace"], kwargs["MetricName"], dimensions), [])
        statistic = kwargs["Statistics"][0]
        return {"Datapoints": [{statistic: value, "Unit": "None"} for value in values]}
