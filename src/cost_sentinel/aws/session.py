"""Build boto3 sessions and the bundle of clients the collectors use."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

import boto3
from botocore.config import Config

logger = logging.getLogger(__name__)

_RETRY_CONFIG = Config(
    retries={"max_attempts": 8, "mode": "adaptive"}, user_agent_extra="cost-sentinel"
)
"""Adaptive retries keep a scan of a large account from tripping API throttling."""


def build_session(profile: str | None = None, region: str | None = None) -> boto3.Session:
    """Create a boto3 session for ``profile`` (default credential chain when None)."""
    kwargs: dict[str, Any] = {}
    if profile:
        kwargs["profile_name"] = profile
    if region:
        kwargs["region_name"] = region
    session = boto3.Session(**kwargs)
    logger.debug(
        "boto3 session for profile=%s region=%s", profile or "default", session.region_name
    )
    return session


@dataclass
class AwsClients:
    """Every AWS client a scan needs, created once and shared by the collectors."""

    region: str
    ec2: Any
    cloudwatch: Any
    elbv2: Any
    rds: Any
    s3: Any
    ce: Any

    @classmethod
    def from_session(cls, session: boto3.Session, region: str | None = None) -> AwsClients:
        """Create all clients in ``region`` (Cost Explorer always talks to us-east-1)."""
        resolved = region or session.region_name
        if not resolved:
            raise ValueError("no AWS region configured; pass --region or set AWS_REGION")
        return cls(
            region=resolved,
            ec2=session.client("ec2", region_name=resolved, config=_RETRY_CONFIG),
            cloudwatch=session.client("cloudwatch", region_name=resolved, config=_RETRY_CONFIG),
            elbv2=session.client("elbv2", region_name=resolved, config=_RETRY_CONFIG),
            rds=session.client("rds", region_name=resolved, config=_RETRY_CONFIG),
            s3=session.client("s3", region_name=resolved, config=_RETRY_CONFIG),
            ce=session.client("ce", region_name="us-east-1", config=_RETRY_CONFIG),
        )
