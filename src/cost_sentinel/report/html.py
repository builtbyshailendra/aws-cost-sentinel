"""Self-contained HTML report writer (inline CSS, no scripts, no external assets)."""

from __future__ import annotations

from html import escape

from cost_sentinel.models import CostReport, ScanResult
from cost_sentinel.report.markdown import (
    TOP_SERVICES,
    headline_overlap,
    money,
    report_notes,
    savings_cell,
)

_CSS = """
:root { --bg: #ffffff; --fg: #1f2933; --muted: #52606d; --line: #e4e7eb; --accent: #0b6e4f;
        --high: #c0392b; --medium: #b9770e; --low: #5d6d7e; --card: #f5f7fa; }
@media (prefers-color-scheme: dark) {
  :root { --bg: #111418; --fg: #e6e8eb; --muted: #9aa5b1; --line: #2b313a; --accent: #4cc38a;
          --high: #ff6b5b; --medium: #f2b134; --low: #9aa5b1; --card: #1a1f26; }
}
* { box-sizing: border-box; }
body { margin: 0; padding: 24px 16px; background: var(--bg); color: var(--fg);
       font: 15px/1.5 -apple-system, "Segoe UI", Roboto, Helvetica, Arial, sans-serif; }
main { max-width: 1100px; margin: 0 auto; }
h1 { font-size: 1.6rem; margin: 0 0 4px; }
h2 { font-size: 1.2rem; margin: 32px 0 8px; border-bottom: 1px solid var(--line);
     padding-bottom: 4px; }
h3 { font-size: 1.05rem; margin: 24px 0 8px; }
.meta { color: var(--muted); font-size: 0.9rem; }
.hero { background: var(--card); border: 1px solid var(--line); border-radius: 8px;
        padding: 16px 20px; margin: 16px 0 8px; }
.hero strong { font-size: 1.7rem; color: var(--accent); }
table { width: 100%; border-collapse: collapse; margin: 8px 0 16px; font-size: 0.92rem; }
th, td { border-bottom: 1px solid var(--line); padding: 6px 8px; text-align: left;
         vertical-align: top; }
th { color: var(--muted); font-weight: 600; }
td.num, th.num { text-align: right; white-space: nowrap; }
code { font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; font-size: 0.88em; }
.badge { display: inline-block; padding: 1px 8px; border-radius: 10px; font-size: 0.8rem;
         color: #fff; }
.badge.high { background: var(--high); } .badge.medium { background: var(--medium); }
.badge.low { background: var(--low); }
ul.notes { color: var(--muted); font-size: 0.9rem; }
@media (max-width: 700px) { table { display: block; overflow-x: auto; } }
"""


def _badge(level: str) -> str:
    return f'<span class="badge {escape(level)}">{escape(level)}</span>'


def _spend_section(report: CostReport) -> list[str]:
    parts = [
        f"<h2>Spend by service ({escape(report.periods[0])} to {escape(report.periods[-1])})</h2>",
        "<table><thead><tr><th>Service</th>"
        f'<th class="num">{escape(report.periods[-1])}</th>'
        '<th class="num">Avg of earlier months</th><th class="num">Trend</th>'
        '<th class="num">Total</th></tr></thead><tbody>',
    ]
    for service, total in report.top_services(TOP_SERVICES):
        latest = report.latest(service)
        average = report.trailing_average(service)
        if average is None:
            avg_text, trend = "n/a", "n/a"
        else:
            avg_text = money(average)
            trend = f"{(latest - average) / average * 100:+.0f}%" if average > 0 else "n/a"
        parts.append(
            f'<tr><td>{escape(service)}</td><td class="num">{money(latest)}</td>'
            f'<td class="num">{avg_text}</td><td class="num">{trend}</td>'
            f'<td class="num">{money(total)}</td></tr>'
        )
    totals = report.total_by_period()
    earlier = totals[:-1]
    avg_total = money(sum(earlier) / len(earlier)) if earlier else "n/a"
    parts.append(
        f'<tr><th>All services</th><th class="num">{money(totals[-1])}</th>'
        f'<th class="num">{avg_total}</th><th></th><th class="num">{money(sum(totals))}</th></tr>'
    )
    parts.append("</tbody></table>")
    return parts


def render_html(result: ScanResult) -> str:
    """Render the full HTML report as a single document."""
    by_confidence = result.by_confidence()
    stamp = result.generated_at.strftime("%Y-%m-%d %H:%M UTC")
    source = escape(result.source) + (
        f" ({escape(result.source_detail)})" if result.source_detail else ""
    )
    parts = [
        "<!DOCTYPE html>",
        '<html lang="en"><head><meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width, initial-scale=1">',
        "<title>AWS Cost Sentinel report</title>",
        f"<style>{_CSS}</style></head><body><main>",
        "<h1>AWS Cost Sentinel report</h1>",
        f'<p class="meta">Generated {escape(stamp)} &middot; region '
        f"<code>{escape(result.inventory.region)}</code> &middot; source {source}</p>",
        '<div class="hero">Estimated monthly savings '
        f"<strong>{money(result.total_savings)}</strong><br>"
        f"{len(result.findings)} finding(s): {by_confidence['high']} high, "
        f"{by_confidence['medium']} medium, {by_confidence['low']} low confidence"
        + (f"<br>{escape(headline_overlap(result))}" if headline_overlap(result) else "")
        + "</div>",
    ]

    if result.findings:
        parts.append("<h2>Summary by check</h2>")
        parts.append(
            '<table><thead><tr><th>Check</th><th class="num">Findings</th>'
            '<th class="num">Est. monthly savings</th></tr></thead><tbody>'
        )
        for row in result.summaries():
            parts.append(
                f"<tr><td>{escape(row.title)} <code>{escape(row.check_id)}</code></td>"
                f'<td class="num">{row.findings}</td>'
                f'<td class="num">{money(row.estimated_monthly_savings)}</td></tr>'
            )
        parts.append("</tbody></table><h2>Findings</h2>")
        for row in result.summaries():
            parts.append(
                f"<h3>{escape(row.title)} ({money(row.estimated_monthly_savings)}/month)</h3>"
            )
            parts.append(
                "<table><thead><tr><th>Resource</th><th>Region</th>"
                '<th class="num">Monthly cost</th><th class="num">Savings</th>'
                "<th>Confidence</th><th>Why</th><th>Recommendation</th></tr></thead><tbody>"
            )
            for finding in result.findings_for(row.check_id):
                parts.append(
                    f"<tr><td><code>{escape(finding.resource_id)}</code></td>"
                    f"<td>{escape(finding.region)}</td>"
                    f'<td class="num">{money(finding.monthly_cost)}</td>'
                    f'<td class="num">{escape(savings_cell(finding))}</td>'
                    f"<td>{_badge(finding.confidence.value)}</td>"
                    f"<td>{escape(finding.reason)}</td>"
                    f"<td>{escape(finding.recommendation)}</td></tr>"
                )
            parts.append("</tbody></table>")
    else:
        parts.append("<p>No waste found by the selected checks.</p>")

    report = result.inventory.cost_report
    if report is not None and report.periods:
        parts.extend(_spend_section(report))

    if result.source == "live":
        counts = result.inventory.counts()
        parts.append("<h2>Resources scanned</h2><p>")
        parts.append(
            escape(", ".join(f"{value} {key.replace('_', ' ')}" for key, value in counts.items()))
        )
        parts.append("</p>")

    if result.inventory.errors:
        parts.append("<h2>Skipped</h2><ul>")
        parts.extend(f"<li>{escape(error)}</li>" for error in result.inventory.errors)
        parts.append("</ul>")

    parts.append('<h2>Notes</h2><ul class="notes">')
    parts.extend(f"<li>{escape(note)}</li>" for note in report_notes(result))
    parts.append("</ul></main></body></html>")
    return "\n".join(parts)
