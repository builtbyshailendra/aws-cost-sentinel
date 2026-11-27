"""Report writers: Markdown, self-contained HTML and JSON."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from cost_sentinel.models import CostSentinelError, ScanResult
from cost_sentinel.report.html import render_html
from cost_sentinel.report.json_writer import render_json
from cost_sentinel.report.markdown import render_markdown

FORMATS: dict[str, Callable[[ScanResult], str]] = {
    "markdown": render_markdown,
    "md": render_markdown,
    "html": render_html,
    "json": render_json,
}

EXTENSIONS: dict[str, str] = {"markdown": ".md", "md": ".md", "html": ".html", "json": ".json"}


class UnknownFormatError(CostSentinelError):
    """The requested report format does not exist."""


def render(result: ScanResult, fmt: str) -> str:
    """Render ``result`` in ``fmt`` (``markdown``/``md``, ``html`` or ``json``)."""
    try:
        writer = FORMATS[fmt.lower()]
    except KeyError as exc:
        raise UnknownFormatError(
            f"unknown format {fmt!r}; choose one of {', '.join(sorted(set(FORMATS)))}"
        ) from exc
    return writer(result)


def write_report(result: ScanResult, fmt: str, path: str | Path) -> Path:
    """Render and write the report to ``path`` (UTF-8), returning the path."""
    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(render(result, fmt), encoding="utf-8", newline="\n")
    return output


__all__ = [
    "EXTENSIONS",
    "FORMATS",
    "UnknownFormatError",
    "render",
    "render_html",
    "render_json",
    "render_markdown",
    "write_report",
]
