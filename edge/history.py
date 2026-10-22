"""Snapshot history for dashboard trends.

Every dashboard run appends one row per (product, TPX market) to a JSONL file
so price and edge trends build up over time. Read-only with respect to the
marketplaces: this only writes a local file.
"""
from __future__ import annotations

import json
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Sequence

from .edge import Assessment


def snapshot_row(a: Assessment, at: datetime, source: str) -> dict:
    arb = a.best_arb
    return {
        "at": at.isoformat(),
        "source": source,
        "product_id": a.tpx.product_id,
        "product": a.product_name,
        "market": a.tpx_market,
        "tpx_bid": a.tpx.best_bid,
        "tpx_ask": a.tpx.best_ask,
        "dn_bid": a.dn.best_bid if a.dn else None,
        "dn_ask": a.dn.best_ask if a.dn else None,
        "fair_value": a.fv.fair_value_cents,
        "edge_per_unit": float(arb.edge_per_unit_cents) if arb and arb.edge_per_unit_cents is not None else None,
        "edge_label": arb.label if arb else None,
    }


def append(path: Path, ranked: Sequence[Assessment], source: str, at: Optional[datetime] = None) -> list[dict]:
    at = at or datetime.now(timezone.utc)
    rows = [snapshot_row(a, at, source) for a in ranked]
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")
    return rows


def load(path: Path, source: Optional[str] = None) -> dict[tuple[str, str], list[dict]]:
    """History keyed by (product_id, market), oldest first. Demo and live rows are
    kept apart by `source` so sample data never leaks into a live trend."""
    out: dict[tuple[str, str], list[dict]] = defaultdict(list)
    if not path.exists():
        return out
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            r = json.loads(line)
        except json.JSONDecodeError:
            continue
        if source and r.get("source") != source:
            continue
        out[(r.get("product_id"), r.get("market"))].append(r)
    for rows in out.values():
        rows.sort(key=lambda r: r.get("at", ""))
    return out
