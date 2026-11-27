"""JSON report writer: a stable, machine-readable shape for dashboards and gates."""

from __future__ import annotations

import json
from typing import Any

from cost_sentinel.models import ScanResult
from cost_sentinel.pricing import PRICE_TABLE_VERSION


def to_document(result: ScanResult) -> dict[str, Any]:
    """The JSON document as a dict (``schema_version`` is bumped on breaking changes)."""
    document = result.to_dict()
    document["pricing"] = {
        "table_version": PRICE_TABLE_VERSION,
        "note": "Bundled us-east-1 list prices with a regional multiplier; estimates only.",
    }
    report = result.inventory.cost_report
    if report is not None:
        document["cost_report"] = {
            "source": report.source,
            "currency": report.currency,
            "periods": list(report.periods),
            "services": {
                name: [round(value, 2) for value in values]
                for name, values in report.services.items()
            },
            "totals": [round(value, 2) for value in report.total_by_period()],
        }
    return document


def render_json(result: ScanResult) -> str:
    """Render the report as pretty-printed JSON."""
    return json.dumps(to_document(result), indent=2, sort_keys=False) + "\n"
