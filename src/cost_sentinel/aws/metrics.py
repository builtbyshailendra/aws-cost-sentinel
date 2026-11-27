"""Small CloudWatch wrapper that answers "what was the average/sum over the window?"."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any

from botocore.exceptions import BotoCoreError, ClientError

logger = logging.getLogger(__name__)

MAX_DATAPOINTS = 1440
"""CloudWatch rejects GetMetricStatistics calls that would return more points than this."""


def choose_period(lookback_days: int) -> int:
    """Pick the smallest period that keeps the call under the datapoint limit."""
    seconds = lookback_days * 86400
    for period in (300, 3600, 21600, 86400):
        if seconds / period <= MAX_DATAPOINTS:
            return period
    return 86400


class MetricReader:
    """Read one statistic for one metric over a fixed window."""

    def __init__(self, cloudwatch: Any, end: datetime, lookback_days: int) -> None:
        self._cloudwatch = cloudwatch
        self.end = end
        self.start = end - timedelta(days=lookback_days)
        self.period = choose_period(lookback_days)

    def statistic(
        self,
        namespace: str,
        metric_name: str,
        dimensions: dict[str, str],
        statistic: str = "Average",
        *,
        latest: bool = False,
    ) -> float | None:
        """Average (mean of datapoints) or Sum (total of datapoints) of a metric.

        With ``latest=True`` the most recent datapoint is returned instead, which is
        the right reading for gauges such as S3 ``BucketSizeBytes`` (reported daily).
        Returns None when CloudWatch has no datapoints for the window, or when the
        call fails; the caller decides whether "unknown" should produce a finding.
        """
        try:
            response = self._cloudwatch.get_metric_statistics(
                Namespace=namespace,
                MetricName=metric_name,
                Dimensions=[{"Name": name, "Value": value} for name, value in dimensions.items()],
                StartTime=self.start,
                EndTime=self.end,
                Period=self.period,
                Statistics=[statistic],
            )
        except (ClientError, BotoCoreError) as exc:
            logger.warning(
                "CloudWatch %s/%s for %s failed: %s", namespace, metric_name, dimensions, exc
            )
            return None
        datapoints = [point for point in response.get("Datapoints", []) if statistic in point]
        if not datapoints:
            logger.debug("no %s datapoints for %s %s", metric_name, namespace, dimensions)
            return None
        if latest:
            if all("Timestamp" in point for point in datapoints):
                datapoints.sort(key=lambda point: point["Timestamp"])
            return float(datapoints[-1][statistic])
        points = [point[statistic] for point in datapoints]
        if statistic == "Sum":
            return float(sum(points))
        return float(sum(points) / len(points))
