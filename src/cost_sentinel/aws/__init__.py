"""boto3-facing code: sessions, CloudWatch metrics, Cost Explorer and inventory collectors."""

from __future__ import annotations

from cost_sentinel.aws.collectors import collect_inventory
from cost_sentinel.aws.cost_explorer import fetch_cost_report
from cost_sentinel.aws.session import AwsClients, build_session

__all__ = ["AwsClients", "build_session", "collect_inventory", "fetch_cost_report"]
