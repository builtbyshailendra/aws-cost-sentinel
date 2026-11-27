"""Run the checks against an inventory and assemble a :class:`ScanResult`."""

from __future__ import annotations

import dataclasses
import logging
from datetime import datetime
from pathlib import Path

from cost_sentinel.aws.collectors import CollectionError, collect_inventory
from cost_sentinel.aws.session import AwsClients
from cost_sentinel.checks import BaseCheck, get_checks
from cost_sentinel.csv_loader import load_cost_explorer_csv
from cost_sentinel.models import Finding, Inventory, ScanConfig, ScanResult

logger = logging.getLogger(__name__)


def skip_reason(check: BaseCheck, inventory: Inventory) -> str | None:
    """Why ``check`` cannot run on ``inventory``, or None when it can."""
    if check.needs_inventory and not inventory.resource_data:
        return (
            "needs a resource inventory, which a Cost Explorer export does not have; "
            "run 'cost-sentinel scan' for this check"
        )
    if check.needs_cost_report and inventory.cost_report is None:
        return "needs Cost Explorer data, which was not collected"
    return None


def run_checks(
    inventory: Inventory,
    config: ScanConfig,
    checks: list[BaseCheck] | None = None,
    skipped: dict[str, str] | None = None,
) -> list[Finding]:
    """Run ``checks`` (default: those selected by ``config.checks``) and merge the findings.

    Checks that cannot run on this inventory are not called; when ``skipped`` is
    given, their ids are recorded in it with the reason.
    """
    selected = checks if checks is not None else get_checks(config.checks)
    findings: list[Finding] = []
    for check in selected:
        reason = skip_reason(check, inventory)
        if reason is not None:
            logger.debug("skipping %s: %s", check.id, reason)
            if skipped is not None:
                skipped[check.id] = reason
            continue
        produced = check.run(inventory, config)
        logger.info("%s produced %d finding(s)", check.id, len(produced))
        findings.extend(produced)
    return findings


def resolve_overlaps(findings: list[Finding]) -> list[Finding]:
    """Count each billed resource once in the savings total.

    Two findings overlap when they name the same resource (region, type and id),
    for example "delete this volume" and "convert this volume to gp3": only one of
    the fixes can be applied, so only the larger saving is real. The other findings
    stay in the report with ``counted_in_total=False`` and ``details["overlaps_with"]``
    naming the check whose saving was counted.
    """
    groups: dict[tuple[str, str, str], list[Finding]] = {}
    for finding in findings:
        groups.setdefault(finding.resource_key, []).append(finding)
    resolved: list[Finding] = []
    for group in groups.values():
        if len(group) == 1:
            resolved.append(group[0])
            continue
        winner = max(group, key=lambda item: (item.estimated_monthly_savings, item.confidence.rank))
        for finding in group:
            if finding is winner:
                resolved.append(finding)
                continue
            logger.info(
                "%s on %s overlaps %s; its saving is not added to the total",
                finding.check_id,
                finding.resource_id,
                winner.check_id,
            )
            resolved.append(
                dataclasses.replace(
                    finding,
                    counted_in_total=False,
                    details={**finding.details, "overlaps_with": winner.check_id},
                )
            )
    return resolved


def _order(findings: list[Finding]) -> list[Finding]:
    return sorted(
        findings,
        key=lambda item: (item.estimated_monthly_savings, item.confidence.rank, item.resource_id),
        reverse=True,
    )


def run_scan(
    inventory: Inventory,
    config: ScanConfig,
    *,
    source: str,
    source_detail: str = "",
    generated_at: datetime | None = None,
    checks: list[BaseCheck] | None = None,
) -> ScanResult:
    """Run the configured checks on ``inventory`` and return the sorted result."""
    selected = checks if checks is not None else get_checks(config.checks)
    skipped: dict[str, str] = {}
    findings = run_checks(inventory, config, selected, skipped)
    if config.min_savings > 0:
        before = len(findings)
        findings = [f for f in findings if f.estimated_monthly_savings >= config.min_savings]
        logger.info("dropped %d finding(s) below min_savings", before - len(findings))
    return ScanResult(
        findings=_order(resolve_overlaps(findings)),
        inventory=inventory,
        config=config,
        generated_at=generated_at or config.now,
        source=source,
        source_detail=source_detail,
        check_titles={check.id: check.title for check in selected},
        skipped_checks=skipped,
    )


class IncompleteScanError(CollectionError):
    """``strict`` was requested and at least one service could not be collected."""


def scan_live(
    clients: AwsClients,
    config: ScanConfig,
    *,
    profile: str | None = None,
    include_cost_report: bool = True,
    strict: bool = False,
) -> ScanResult:
    """Collect the inventory from AWS and run every check.

    Raises :class:`CollectionError` when no resource could be collected at all
    (missing or invalid credentials, no network), and :class:`IncompleteScanError`
    when ``strict`` is set and any single service was skipped.
    """
    inventory = collect_inventory(clients, config, include_cost_report=include_cost_report)
    if strict and inventory.errors:
        raise IncompleteScanError(
            f"{len(inventory.errors)} service(s) could not be collected and --strict is set: "
            + "; ".join(inventory.errors)
        )
    detail = f"profile={profile or 'default'} region={clients.region}"
    return run_scan(inventory, config, source="live", source_detail=detail)


def scan_csv(path: str | Path, config: ScanConfig) -> ScanResult:
    """Load a Cost Explorer export and run the checks that work on cost data alone."""
    report = load_cost_explorer_csv(path)
    inventory = Inventory.from_cost_report(report, region=config.region)
    inventory.collected_at = config.now
    return run_scan(inventory, config, source="csv", source_detail=Path(path).as_posix())
