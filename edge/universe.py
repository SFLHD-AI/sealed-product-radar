"""The product universe: every sealed product TPX lists, with its 30-day stats,
so the board can offer "hottest" products and a search box without a YAML.

One stats call per product (GET_PRODUCT_MARKET with periodDays=30, no market),
run through the client's rate limiter on a small thread pool, cached on disk
for UNIVERSE_MAX_AGE_S. Read-only.
"""
from __future__ import annotations

import json
import logging
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Optional

from .tpx import queries as Q
from .tpx.catalog import Catalog
from .tpx.client import TPXClient, TPXError

log = logging.getLogger("edge.universe")

UNIVERSE_MAX_AGE_S = 12 * 3600
PAGE = 100


def _int(v) -> Optional[int]:
    try:
        return int(v) if v is not None else None
    except (TypeError, ValueError):
        return None


class Universe:
    def __init__(self, client: TPXClient, catalog: Catalog, cache_dir: Path, per_type: int = 600, workers: int = 6):
        self.client, self.catalog = client, catalog
        self.path = Path(cache_dir) / "universe.json"
        self.per_type, self.workers = per_type, workers

    def load(self, *, refresh: bool = False) -> list[dict]:
        """[{id, name, asset_type, key_id, image, volume_cents, units, trades, last_cents,
        change_pct, fair_value_cents, scanned_at}] sorted by 30-day dollar volume, descending."""
        if not refresh:
            try:
                if time.time() - self.path.stat().st_mtime < UNIVERSE_MAX_AGE_S:
                    return json.loads(self.path.read_text())
            except (OSError, ValueError):
                pass
        rows = self.scan()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(rows))
        return rows

    def load_cached(self) -> list[dict]:
        """The last scan, whatever its age, without touching the network ([] if none)."""
        try:
            return json.loads(self.path.read_text())
        except (OSError, ValueError):
            return []

    def scan(self) -> list[dict]:
        rows: list[dict] = []
        for vertical in ("sealed_wax", "sealed_tcg"):
            try:
                at_id = self.catalog.asset_type_ids(vertical)[vertical]
                key_id = self.catalog.resolve_condition_key(at_id)
            except (TPXError, KeyError, RuntimeError) as exc:
                log.warning("universe: %s skipped: %s", vertical, exc)
                continue
            prods = self._list_products(at_id, vertical)
            log.info("universe: %s %d products, fetching 30d stats", vertical, len(prods))
            with ThreadPoolExecutor(self.workers) as ex:
                rows += [r for r in ex.map(lambda p: self._stats(p, key_id), prods) if r]
        rows.sort(key=lambda r: -(r.get("volume_cents") or 0))
        return rows

    def _list_products(self, at_id: str, vertical: str) -> list[dict]:
        out: list[dict] = []
        for off in range(0, self.per_type, PAGE):
            payload = self.client.execute(Q.FILTER_BUILDER_SEARCH, {"input": {
                "assetTypeId": at_id, "skipFacets": True, "previewLimit": PAGE, "previewOffset": off}}, cache_ttl=3600)
            page = (payload or {}).get("preview") or []
            for p in page:
                data = p.get("data") or {}
                variants = data.get("image_variants") or {}
                out.append({"id": p["id"], "name": p.get("name") or "", "asset_type": vertical,
                            "set": p.get("assetName"), "brand": data.get("brand"), "format": data.get("format"),
                            "image": variants.get("200_x_200") or (data.get("image_urls") or [None])[0],
                            "fair_value_cents": _int(p.get("fairValueCents"))})
            if len(page) < PAGE:
                break
        return out

    def _stats(self, p: dict, key_id: str) -> Optional[dict]:
        try:
            payload = self.client.execute(Q.GET_PRODUCT_MARKET, {"input": {
                "assetConfigurationId": p["id"], "keyId": key_id, "periodDays": 30}}, cache_ttl=1800) or {}
        except TPXError as exc:
            log.debug("universe: stats failed for %s: %s", p["name"], exc)
            return {**p, "key_id": key_id, "volume_cents": 0, "units": 0, "trades": 0}
        st = payload.get("stats") or {}
        return {**p, "key_id": key_id,
                "volume_cents": _int(st.get("volumeCents")) or 0, "units": _int(st.get("unitVolume")) or 0,
                "trades": _int(st.get("tradeCount")) or 0, "last_cents": _int(st.get("lastPriceCents")),
                "change_pct": st.get("priceChangePercent"), "scanned_at": time.time()}


def hottest(rows: list[dict], n: int = 20, asset_type: Optional[str] = None) -> list[dict]:
    pool = [r for r in rows if not asset_type or r.get("asset_type") == asset_type]
    return sorted(pool, key=lambda r: -(r.get("volume_cents") or 0))[:n]
