"""Registry of every check the scanner knows about."""

from __future__ import annotations

from collections.abc import Iterable

from cost_sentinel.checks.base import BaseCheck
from cost_sentinel.checks.compute import IdleEc2Check, SavingsPlanCheck
from cost_sentinel.checks.database import IdleRdsCheck
from cost_sentinel.checks.network import (
    IdleLoadBalancerCheck,
    NatGatewayCheck,
    UnusedElasticIpCheck,
)
from cost_sentinel.checks.spend import SpendGrowthCheck
from cost_sentinel.checks.storage import (
    Gp2ToGp3Check,
    OldSnapshotCheck,
    S3LifecycleCheck,
    UnattachedEbsCheck,
)
from cost_sentinel.models import CostSentinelError

ALL_CHECKS: tuple[BaseCheck, ...] = (
    IdleEc2Check(),
    UnattachedEbsCheck(),
    OldSnapshotCheck(),
    UnusedElasticIpCheck(),
    IdleLoadBalancerCheck(),
    NatGatewayCheck(),
    Gp2ToGp3Check(),
    IdleRdsCheck(),
    S3LifecycleCheck(),
    SavingsPlanCheck(),
    SpendGrowthCheck(),
)


class UnknownCheckError(CostSentinelError):
    """A check id was requested that does not exist."""


def check_ids() -> list[str]:
    """Ids of every registered check, in registry order."""
    return [check.id for check in ALL_CHECKS]


def get_checks(ids: Iterable[str] | None = None) -> list[BaseCheck]:
    """Return the requested checks (all of them when ``ids`` is None)."""
    if ids is None:
        return list(ALL_CHECKS)
    wanted = list(ids)
    by_id = {check.id: check for check in ALL_CHECKS}
    unknown = [check_id for check_id in wanted if check_id not in by_id]
    if unknown:
        raise UnknownCheckError(
            f"unknown check(s): {', '.join(unknown)}; available: {', '.join(by_id)}"
        )
    return [by_id[check_id] for check_id in wanted]


__all__ = [
    "ALL_CHECKS",
    "BaseCheck",
    "Gp2ToGp3Check",
    "IdleEc2Check",
    "IdleLoadBalancerCheck",
    "IdleRdsCheck",
    "NatGatewayCheck",
    "OldSnapshotCheck",
    "S3LifecycleCheck",
    "SavingsPlanCheck",
    "SpendGrowthCheck",
    "UnattachedEbsCheck",
    "UnknownCheckError",
    "UnusedElasticIpCheck",
    "check_ids",
    "get_checks",
]
