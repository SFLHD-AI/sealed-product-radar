"""CSV backend. Two layouts are accepted (header row required):

Ladder layout - one row per offer (best for a pasted product page):
    product,side,price,qty[,member][,observed_at]
    2024 Topps Chrome Baseball Hobby Box,bid,245,3,m1829,2026-10-20
    2024 Topps Chrome Baseball Hobby Box,ask,262,1,m0412,

Wide layout - one row per product:
    product,bid,bid_qty,ask,ask_qty[,observed_at]
    2024 Topps Chrome Baseball Hobby Box,245,3,262,1,

Prices in dollars (with or without $). `product` is matched to the TPX
product via matching.py (fuzzy + mappings.yaml), or exactly when you pass
--dn-name. Pass "-" as the path to read a paste from stdin.
"""
from __future__ import annotations

import csv
import io
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .base import DealernetLevel, DealernetQuote, DealernetSource, dollars_to_cents

LADDER_COLS = {"product", "side", "price", "qty"}
WIDE_COLS = {"product", "bid", "ask"}


def _dt(v: Optional[str]) -> Optional[datetime]:
    if not v or not v.strip():
        return None
    s = v.strip().replace("Z", "+00:00")
    try:
        d = datetime.fromisoformat(s)
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def parse_dealernet_csv(text: str, source: str = "csv") -> dict[str, DealernetQuote]:
    reader = csv.DictReader(io.StringIO(text.strip()))
    if not reader.fieldnames:
        return {}
    cols = {c.strip().lower() for c in reader.fieldnames}
    rows = [{(k or "").strip().lower(): (v or "").strip() for k, v in r.items()} for r in reader]
    quotes: dict[str, DealernetQuote] = {}

    def q_for(name: str) -> DealernetQuote:
        if name not in quotes:
            quotes[name] = DealernetQuote(product=name, source=source)
        return quotes[name]

    if LADDER_COLS <= cols:
        for r in rows:
            if not r.get("product") or not r.get("price"):
                continue
            lvl = DealernetLevel(
                price_cents=dollars_to_cents(r["price"]),
                quantity=int(r.get("qty") or 1),
                member=r.get("member") or None,
                observed_at=_dt(r.get("observed_at")),
            )
            side = r["side"].lower()
            target = q_for(r["product"])
            (target.bids if side.startswith("b") else target.asks).append(lvl)
        return quotes

    if WIDE_COLS <= cols:
        for r in rows:
            if not r.get("product"):
                continue
            q = q_for(r["product"])
            obs = _dt(r.get("observed_at"))
            if r.get("bid"):
                q.bids.append(DealernetLevel(price_cents=dollars_to_cents(r["bid"]),
                                             quantity=int(r.get("bid_qty") or 1), observed_at=obs))
            if r.get("ask"):
                q.asks.append(DealernetLevel(price_cents=dollars_to_cents(r["ask"]),
                                             quantity=int(r.get("ask_qty") or 1), observed_at=obs))
        return quotes

    raise ValueError(
        f"Unrecognised CSV header {sorted(cols)}. Expected ladder columns {sorted(LADDER_COLS)} "
        f"or wide columns {sorted(WIDE_COLS)}."
    )


class CSVSource(DealernetSource):
    name = "csv"

    def __init__(self, path: str | Path, matcher=None):
        self.path = str(path)
        text = sys.stdin.read() if self.path == "-" else Path(self.path).read_text(encoding="utf-8-sig")
        self.quotes = parse_dealernet_csv(text, source=f"csv:{self.path}")
        self.matcher = matcher  # callable(product, candidates) -> best name or None

    def products(self) -> list[str]:
        return list(self.quotes)

    def get_quote(self, product: str, upc: Optional[str] = None) -> Optional[DealernetQuote]:
        if product in self.quotes:
            return self.quotes[product]
        if self.matcher:
            best = self.matcher(product, list(self.quotes))
            if best:
                return self.quotes[best]
        return None
