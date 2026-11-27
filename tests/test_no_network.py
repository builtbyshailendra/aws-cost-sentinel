"""The suite must never reach real AWS (or anything else off the machine)."""

from __future__ import annotations

import socket

import boto3
import pytest
from botocore.exceptions import HTTPClientError


def test_outbound_connections_are_blocked() -> None:
    with pytest.raises(RuntimeError, match=r"network|resolve"):
        socket.create_connection(("ec2.us-east-1.amazonaws.com", 443), timeout=1)


def test_a_boto3_call_outside_moto_cannot_reach_aws() -> None:
    client = boto3.client("sts", region_name="us-east-1")
    with pytest.raises(HTTPClientError, match=r"amazonaws\.com"):
        client.get_caller_identity()
