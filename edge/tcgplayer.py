"""TCGplayer market price, used as a reference for sealed TCG products.

Read-only: OAuth client-credentials token + GET /pricing/product. Nothing here
lists, buys or touches inventory.

* Auth: POST {api}/token with grant_type=client_credentials, client_id = public
  key, client_secret = private key -> bearer token (cached on disk until it
  expires, roughly two weeks).
* Prices: GET {api}/pricing/product/{ids} -> per productId and subTypeName
  (sealed is "Normal"): marketPrice, lowPrice, midPrice, directLowPrice (USD).
* TPX -> TCGplayer product id comes from TPX's own SEALED_TCG catalog feed
  (externalIds.TCG_PLAYER_ID), so there is no name matching. Sports wax has no
  TCGplayer listing and stays blank.
"""
from __future__ import annotations

import gzip
import json
import logging
import time
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Iterable, Optional

import httpx

log = logging.getLogger("edge.tcgplayer")

FEED_MAX_AGE_S = 24 * 3600


@dataclass
class TCGPrice:
    product_id: str
    market_cents: Optional[int]
    low_cents: Optional[int]
    mid_cents: Optional[int]
    direct_low_cents: Optional[int]
    sub_type: Optional[str] = None
    # From the product page (no API key): the most recent sale and the price trend.
    last_sale_cents: Optional[int] = None
    last_sale_at: Optional[str] = None  # ISO text as TCGplayer gives it
    trend: Optional[list] = None  # [{t, market, sold}] oldest first, roughly the last 90 days
    trend_pct: Optional[float] = None  # market price now vs ~30 days ago, %
    source: str = "api"

    def as_payload(self) -> dict:
        return {"market": self.market_cents, "low": self.low_cents, "last": self.last_sale_cents,
                "last_at": self.last_sale_at, "trend": self.trend, "chg": self.trend_pct, "source": self.source}


def _cents(v) -> Optional[int]:
    if v is None:
        return None
    return int((Decimal(str(v)) * 100).quantize(Decimal("1")))


def pct_of(price_cents: Optional[int], ref_cents: Optional[int]) -> Optional[Decimal]:
    """price as % of the reference (100 == at market). None when either is missing."""
    if price_cents is None or not ref_cents:
        return None
    return (Decimal(price_cents) * 100 / Decimal(ref_cents)).quantize(Decimal("0.1"))


class TCGplayerClient:
    def __init__(self, public_key: str, private_key: str, api_url: str, cache_dir: Path, *,
                 transport: Optional[httpx.BaseTransport] = None, timeout_s: float = 20.0):
        if not public_key or not private_key:
            raise RuntimeError("TCGPLAYER_PUBLIC_KEY and TCGPLAYER_PRIVATE_KEY must both be set.")
        self.api = api_url.rstrip("/")
        self._pub, self._priv = public_key, private_key
        self._token_path = Path(cache_dir) / "tcgplayer_token.json"
        self._http = httpx.Client(timeout=timeout_s, transport=transport)

    def close(self) -> None:
        self._http.close()

    def _token(self) -> str:
        try:
            cached = json.loads(self._token_path.read_text())
            if cached.get("expires_at", 0) > time.time() + 300:
                return cached["access_token"]
        except (OSError, ValueError, KeyError):
            pass
        r = self._http.post(f"{self.api}/token", data={
            "grant_type": "client_credentials", "client_id": self._pub, "client_secret": self._priv})
        if r.status_code in (400, 401, 403):
            raise RuntimeError(f"TCGplayer rejected the API keys (HTTP {r.status_code}).")
        r.raise_for_status()
        body = r.json()
        tok = body["access_token"]
        self._token_path.parent.mkdir(parents=True, exist_ok=True)
        self._token_path.write_text(json.dumps({"access_token": tok,
                                                "expires_at": time.time() + int(body.get("expires_in", 3600))}))
        return tok

    def prices(self, product_ids: Iterable[str]) -> dict[str, TCGPrice]:
        ids = [str(i) for i in dict.fromkeys(product_ids) if i]
        out: dict[str, TCGPrice] = {}
        for i in range(0, len(ids), 100):  # API takes comma-separated ids; keep URLs short
            chunk = ids[i:i + 100]
            r = self._http.get(f"{self.api}/pricing/product/{','.join(chunk)}",
                               headers={"Authorization": f"bearer {self._token()}", "Accept": "application/json"})
            if r.status_code == 404:  # none of the ids priced
                continue
            r.raise_for_status()
            for row in r.json().get("results") or []:
                pid = str(row.get("productId"))
                # Sealed products price under "Normal"; keep the first priced row otherwise.
                if pid in out and out[pid].sub_type == "Normal":
                    continue
                if row.get("marketPrice") is None and row.get("lowPrice") is None and pid in out:
                    continue
                out[pid] = TCGPrice(pid, _cents(row.get("marketPrice")), _cents(row.get("lowPrice")),
                                    _cents(row.get("midPrice")), _cents(row.get("directLowPrice")),
                                    row.get("subTypeName"))
        return out


def _first(d: dict, *keys):
    for k in keys:
        if isinstance(d, dict) and d.get(k) is not None:
            return d[k]
    return None


class TCGplayerPages:
    """TCGplayer prices without API keys: the same public endpoints the product
    page itself loads (market price, latest sales, price history). Read-only GETs
    plus the page's own POST for the sales list. Rate-limited and cached on disk
    for `cache_s` so a refresh asks once per product. No login, no cart, nothing
    written to TCGplayer."""

    MP = "https://mpapi.tcgplayer.com/v2/product/{pid}"
    HISTORY = "https://infinite-api.tcgplayer.com/price/history/{pid}/detailed?range=quarter"
    PAGE = "https://www.tcgplayer.com/product/{pid}"

    def __init__(self, cache_dir: Path, *, transport: Optional[httpx.BaseTransport] = None,
                 timeout_s: float = 20.0, min_interval_s: float = 1.0, cache_s: int = 1800):
        self.cache_dir = Path(cache_dir) / "tcgplayer_pages"
        self.min_interval_s, self.cache_s = min_interval_s, cache_s
        self._last = 0.0
        self.dead: Optional[str] = None  # set when the network refuses TCGplayer; the run then stops asking
        self._http = httpx.Client(timeout=timeout_s, transport=transport, follow_redirects=True, headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
            "Accept": "application/json, text/plain, */*", "Accept-Language": "en-US,en;q=0.9",
            "Origin": "https://www.tcgplayer.com", "Referer": "https://www.tcgplayer.com/"})

    def close(self) -> None:
        self._http.close()

    def _wait(self) -> None:
        gap = self.min_interval_s - (time.time() - self._last)
        if gap > 0:
            time.sleep(gap)
        self._last = time.time()

    def _json(self, method: str, url: str, body: Optional[dict] = None, pid: str = ""):
        if self.dead:
            raise RuntimeError(self.dead)
        self._wait()
        try:  # the sales endpoint answers 403 without the page's own Referer
            r = self._http.request(method, url, json=body, headers={"Referer": self.PAGE.format(pid=pid) if pid else self.PAGE.format(pid="")})
        except httpx.TransportError as exc:
            # A proxy CONNECT 403 or DNS failure means the host is blocked for this run, not this product.
            self.dead = f"TCGplayer unreachable ({exc.__class__.__name__}: {exc}); network policy may block *.tcgplayer.com"
            log.warning(self.dead)
            raise
        r.raise_for_status()
        return r.json()

    def price(self, pid: str) -> Optional[TCGPrice]:
        pid = str(pid)
        path = self.cache_dir / f"{pid}.json"
        try:
            if time.time() - path.stat().st_mtime < self.cache_s:
                d = json.loads(path.read_text())
                return TCGPrice(**d) if d else None
        except (OSError, ValueError, TypeError):
            pass
        if self.dead:
            return None
        out = self._fetch(pid)
        if self.dead:
            return None
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(out.__dict__ if out else None))
        return out

    def _fetch(self, pid: str) -> Optional[TCGPrice]:
        market = low = mid = None
        try:  # price points: one row per printing/condition; sealed is "Normal"
            rows = self._json("GET", self.MP.format(pid=pid) + "/pricepoints")
            rows = rows if isinstance(rows, list) else (rows.get("result") or rows.get("data") or [])
            pick = next((r for r in rows if str(_first(r, "printingType", "variant", "subTypeName") or "").lower() == "normal"), rows[0] if rows else None)
            if pick:
                market, low, mid = _cents(_first(pick, "marketPrice")), _cents(_first(pick, "lowestPrice", "lowPrice")), _cents(_first(pick, "listedMedianPrice", "medianPrice", "midPrice"))
        except Exception as exc:  # noqa: BLE001
            log.info("TCGplayer price points unavailable for %s: %s", pid, exc)
        if market is None:
            try:  # product details carry the headline market price the page shows
                d = self._json("GET", self.MP.format(pid=pid) + "/details")
                d = d.get("result", d) if isinstance(d, dict) else {}
                market = _cents(_first(d, "marketPrice"))
                low = low or _cents(_first(d, "lowestPrice", "lowestPriceWithShipping"))
            except Exception as exc:  # noqa: BLE001
                log.info("TCGplayer details unavailable for %s: %s", pid, exc)
        last_c = last_at = None
        try:
            sales = self._json("POST", self.MP.format(pid=pid) + "/latestsales",
                               {"listingType": "All", "limit": 25, "offset": 0, "variants": [], "conditions": [], "languages": []}, pid)
            rows = sales.get("data") if isinstance(sales, dict) else sales
            rows = [r for r in (rows or []) if _first(r, "purchasePrice", "price") is not None]
            if rows:
                rows.sort(key=lambda r: str(_first(r, "orderDate", "date") or ""), reverse=True)
                last_c, last_at = _cents(_first(rows[0], "purchasePrice", "price")), str(_first(rows[0], "orderDate", "date") or "")[:19] or None
        except Exception as exc:  # noqa: BLE001
            log.info("TCGplayer latest sales unavailable for %s: %s", pid, exc)
        trend, trend_pct = None, None
        try:
            h = self._json("GET", self.HISTORY.format(pid=pid))
            series = h.get("result") if isinstance(h, dict) else h
            series = series or []
            pick = next((r for r in series if str(_first(r, "variant", "printingType") or "").lower() == "normal"), series[0] if series else None)
            buckets = (pick or {}).get("buckets") or []
            trend = [{"t": str(_first(b, "bucketStartDate", "date") or "")[:10], "market": _cents(_first(b, "marketPrice")),
                      "sold": int(Decimal(str(_first(b, "quantitySold", "transactionCount") or 0))),
                      "lo": _cents(_first(b, "lowSalePrice")) or None, "hi": _cents(_first(b, "highSalePrice")) or None}
                     for b in buckets if _first(b, "marketPrice") is not None]
            trend.sort(key=lambda x: x["t"])
            if last_c is None:  # no sales list: the newest bucket with sales gives the latest sale range
                b = next((x for x in reversed(trend) if x["sold"] and x["hi"]), None)
                if b:
                    last_c, last_at = b["hi"], b["t"]
            if len(trend) >= 2:
                now_m = trend[-1]["market"]
                then = next((x for x in reversed(trend) if x["t"] <= _days_ago(30)), trend[0])
                if then["market"]:
                    trend_pct = round((now_m / then["market"] - 1) * 100, 2)
                market = market or now_m
        except Exception as exc:  # noqa: BLE001
            log.info("TCGplayer price history unavailable for %s: %s", pid, exc)
        if market is None and last_c is None and not trend:
            return None
        return TCGPrice(pid, market, low, mid, None, "Normal", last_c, last_at, trend or None, trend_pct, "page")


def _days_ago(n: int) -> str:
    from datetime import datetime, timedelta, timezone
    return (datetime.now(timezone.utc) - timedelta(days=n)).strftime("%Y-%m-%d")


class TCGplayerIdMap:
    """TPX assetConfigurationId -> TCGplayer productId, from TPX's SEALED_TCG catalog feed."""

    def __init__(self, tpx_client, cache_dir: Path):
        from .tpx import queries as Q

        self._q = Q.GET_CATALOG_FEEDS
        self.tpx = tpx_client
        self.path = Path(cache_dir) / "tcgplayer_ids.json"
        self._map: Optional[dict[str, str]] = None

    def get(self, asset_configuration_id: str) -> Optional[str]:
        if self._map is None:
            self._map = self._load()
        return self._map.get(str(asset_configuration_id))

    def _load(self) -> dict[str, str]:
        try:
            if time.time() - self.path.stat().st_mtime < FEED_MAX_AGE_S:
                return json.loads(self.path.read_text())
        except (OSError, ValueError):
            pass
        try:
            payload = self.tpx.execute(self._q, {"input": {"assetTypes": ["SEALED_TCG"]}}, cache_ttl=600)
            feeds = payload.get("feeds") or []
            if not feeds:
                return {}
            raw = httpx.get(feeds[0]["uri"], timeout=120).content
            doc = json.loads(gzip.decompress(raw))
        except Exception as exc:  # noqa: BLE001 - reference data; the dashboard works without it
            log.warning("TPX catalog feed unavailable, no TCGplayer ids: %s", exc)
            return {}
        m = {str(c["assetConfigurationId"]): str(c["externalIds"]["TCG_PLAYER_ID"])
             for a in doc.get("assets") or [] for c in a.get("configurations") or []
             if (c.get("externalIds") or {}).get("TCG_PLAYER_ID")}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(m))
        return m
