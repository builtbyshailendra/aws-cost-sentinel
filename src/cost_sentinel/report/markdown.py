"""Markdown report writer (also what the GitHub Action posts on pull requests)."""

from __future__ import annotations

from cost_sentinel.models import CostReport, Finding, ScanResult
from cost_sentinel.pricing import PRICE_TABLE_VERSION

TOP_SERVICES = 10


def money(amount: float) -> str:
    """``$1,234.56`` formatting."""
    return f"${amount:,.2f}"


def _cell(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", " ")


def spend_table(report: CostReport) -> list[str]:
    """Rows for the top services: latest month, trailing average and trend."""
    lines = [
        f"| Service | {report.periods[-1]} | Avg of earlier months | Trend | Total |",
        "|---|---:|---:|---:|---:|",
    ]
    for service, total in report.top_services(TOP_SERVICES):
        latest = report.latest(service)
        average = report.trailing_average(service)
        if average is None:
            avg_text, trend = "n/a", "n/a"
        else:
            avg_text = money(average)
            trend = f"{(latest - average) / average * 100:+.0f}%" if average > 0 else "n/a"
        lines.append(
            f"| {_cell(service)} | {money(latest)} | {avg_text} | {trend} | {money(total)} |"
        )
    totals = report.total_by_period()
    lines.append(
        f"| **All services** | **{money(totals[-1])}** | "
        f"**{money(sum(totals[:-1]) / len(totals[:-1])) if len(totals) > 1 else 'n/a'}** | "
        f"| **{money(sum(totals))}** |"
    )
    return lines


def report_notes(result: ScanResult) -> list[str]:
    """Plain-text caveats printed at the end of every report format."""
    notes = [
        f"Prices come from the bundled table (version {PRICE_TABLE_VERSION}, us-east-1 list "
        "prices with a regional multiplier). Treat every figure as an estimate."
    ]
    if result.source == "live":
        notes.append(
            f"CloudWatch lookback: {result.config.lookback_days} days; "
            f"EC2 idle CPU threshold: {result.config.ec2_cpu_threshold:g}%; "
            f"snapshot max age: {result.config.snapshot_max_age_days} days."
        )
    else:
        notes.append(
            "A Cost Explorer export has no resource ids, so only the savings-plan and "
            "spend-growth checks run; use 'cost-sentinel scan' for resource-level findings."
        )
    notes.append(
        "The total counts each resource once: when several checks flag the same resource, "
        "only the largest saving is added, and the Savings Plan baseline leaves out idle "
        "instances."
    )
    explicit = result.source != "csv" or result.config.checks is not None
    if result.skipped_checks and explicit:
        notes.append(
            "Checks not run: "
            + "; ".join(f"{check_id} ({why})" for check_id, why in result.skipped_checks.items())
            + "."
        )
    notes.append(
        "Confidence reflects how likely the saving is real, not how precise the number is."
    )
    return notes


def headline_overlap(result: ScanResult) -> str:
    """Sentence explaining savings left out of the total, or an empty string."""
    if result.overlapping_savings <= 0.005:
        return ""
    return (
        f"A further {money(result.overlapping_savings)} is listed but not counted, because "
        "it is an alternative fix for a resource another finding already covers."
    )


def savings_cell(finding: Finding) -> str:
    """Savings text for one finding, marking alternatives left out of the total."""
    amount = money(finding.estimated_monthly_savings)
    if finding.counted_in_total:
        return amount
    other = finding.details.get("overlaps_with", "another check")
    return f"{amount} (not counted; same resource as {other})"


def render_markdown(result: ScanResult) -> str:
    """Render the full Markdown report."""
    by_confidence = result.by_confidence()
    stamp = result.generated_at.strftime("%Y-%m-%d %H:%M UTC")
    lines = [
        "# AWS Cost Sentinel report",
        "",
        f"Generated {stamp} | region `{result.inventory.region}` | source `{result.source}`"
        + (f" ({_cell(result.source_detail)})" if result.source_detail else ""),
        "",
        f"**Estimated monthly savings: {money(result.total_savings)}** across "
        f"{len(result.findings)} finding(s) "
        f"({by_confidence['high']} high, {by_confidence['medium']} medium, "
        f"{by_confidence['low']} low confidence).",
        "",
    ]
    overlap = headline_overlap(result)
    if overlap:
        lines += [overlap, ""]

    if result.findings:
        lines += [
            "## Summary by check",
            "",
            "| Check | Findings | Est. monthly savings |",
            "|---|---:|---:|",
        ]
        for row in result.summaries():
            lines.append(
                f"| {row.title} (`{row.check_id}`) | {row.findings} | "
                f"{money(row.estimated_monthly_savings)} |"
            )
        lines.append("")
        lines.append("## Findings")
        lines.append("")
        for row in result.summaries():
            lines.append(f"### {row.title} ({money(row.estimated_monthly_savings)}/month)")
            lines.append("")
            lines.append(
                "| Resource | Region | Monthly cost | Savings | Confidence | Why | Recommendation |"
            )
            lines.append("|---|---|---:|---:|---|---|---|")
            for finding in result.findings_for(row.check_id):
                lines.append(
                    f"| `{_cell(finding.resource_id)}` | {finding.region} | "
                    f"{money(finding.monthly_cost)} | {savings_cell(finding)} | "
                    f"{finding.confidence.value} | {_cell(finding.reason)} | "
                    f"{_cell(finding.recommendation)} |"
                )
            lines.append("")
    else:
        lines += ["No waste found by the selected checks.", ""]

    report = result.inventory.cost_report
    if report is not None and report.periods:
        lines.append(f"## Spend by service ({report.periods[0]} to {report.periods[-1]})")
        lines.append("")
        lines.extend(spend_table(report))
        lines.append("")

    if result.source == "live":
        counts = result.inventory.counts()
        lines.append("## Resources scanned")
        lines.append("")
        lines.append(", ".join(f"{value} {key.replace('_', ' ')}" for key, value in counts.items()))
        lines.append("")

    if result.inventory.errors:
        lines.append("## Skipped")
        lines.append("")
        lines.extend(f"- {_cell(error)}" for error in result.inventory.errors)
        lines.append("")

    lines += ["## Notes", ""]
    lines.extend(f"- {note}" for note in report_notes(result))
    lines.append("")
    return "\n".join(lines)
