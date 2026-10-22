"""Market data: markets + fees, order book, stats, tape, candles, fair value.

Everything returned is CLEAN cents (pre-fee), exactly as TPX serves it.
BigInt fields arrive as strings; _int() normalises them.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Optional

from ..models import BookLevel, CandleSummary, CostModel, FeeModelKind, Product, VenueQuote, utcnow
from . import queries as Q
from .client import TPXClient, TPXDomainError, TPXGraphQLError

log = logging.getLogger("edge.market")


def _int(v: Any) -> Optional[int]:
    if v is None or v == "":
        return None
    try:
        return int(Decimal(str(v)))
    except Exception:
        return None


def _dec(v: Any) -> Optional[Decimal]:
    if v is None or v == "":
        return None
    try:
        return Decimal(str(v))
    except Exception:
        return None


def _dt(v: Any) -> Optional[datetime]:
    if not v:
        return None
    if isinstance(v, (int, float)) or (isinstance(v, str) and v.isdigit()):
        n = int(v)
        if n > 10_000_000_000:  # ms
            n //= 1000
        return datetime.fromtimestamp(n, tz=timezone.utc)
    try:
        s = str(v).replace("Z", "+00:00")
        d = datetime.fromisoformat(s)
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _iso(d: datetime) -> str:
    return d.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _bar_time(b: dict) -> str:
    return str(b.get("bucket") or b.get("openTime") or "")


class MarketData:
    def __init__(self, client: TPXClient):
        self.client = client

    # --- markets & fees ---------------------------------------------------
    def product_markets(self, product: Product) -> list[dict]:
        payload = self.client.execute(
            Q.GET_PRODUCT_MARKETS,
            {"input": {"assetConfigurationId": product.asset_configuration_id, "keyId": product.key_id}},
            cache_ttl=300,
        )
        # Live shape: [{market {id displayName isVaulted}, canBuy.., associations [{tradingState}]}].
        # Flattened so callers see one dict per venue.
        out = []
        for entry in payload.get("markets") or []:
            m = dict(entry.get("market") or {})
            for perm in ("canBuy", "canSell", "canBid", "canAsk"):
                m[perm] = entry.get(perm)
            states = [a.get("tradingState") for a in entry.get("associations") or [] if a.get("tradingState")]
            m["tradingState"] = states[0] if states else None
            out.append(m)
        return out

    def market_cost_model(self, market_id: str, display_name: str, shipping_in: int, shipping_out: int,
                          is_vaulted: bool) -> CostModel:
        payload = self.client.execute(Q.GET_MARKET, {"input": {"marketId": market_id}}, cache_ttl=3600)
        m = payload.get("market") or {}
        # Fee fields sit directly on configuration.data; feeModel is an enum string.
        data = fm = ((m.get("configuration") or {}).get("data")) or {}
        kind = FeeModelKind.MAKER_TAKER if str(fm.get("feeModel") or "").upper() == "MAKER_TAKER" else FeeModelKind.BUYER_SELLER
        # Vaulted markets settle inside the vault: no shipping on that leg. You
        # still need vaulted units to sell there (flagged in the report).
        return CostModel(
            venue=f"TPX:{display_name}",
            kind=kind,
            buyer_fee_bps=_int(fm.get("buyerFeeBps")) or 0,
            seller_fee_bps=_int(fm.get("sellerFeeBps")) or 0,
            maker_fee_bps=_int(fm.get("makerFeeBps")) or 0,
            taker_fee_bps=_int(fm.get("takerFeeBps")) or 0,
            shipping_in_cents=0 if is_vaulted else shipping_in,
            shipping_out_cents=0 if is_vaulted else shipping_out,
            tick_size_cents=_int(data.get("tickSizeCents")) or 1,
            lot_size=_int(data.get("lotSize")) or 1,
            source="TPX getMarket",
        )

    # --- book -------------------------------------------------------------
    def order_book(self, product: Product, market_id: Optional[str], levels: int = 25) -> dict:
        inp: dict[str, Any] = {"assetConfigurationId": product.asset_configuration_id, "levelsLimit": levels}
        if product.key_id:
            inp["keyId"] = product.key_id
        if market_id:
            inp["marketId"] = market_id
        return self.client.execute(Q.GET_ORDER_BOOK, {"input": inp}, cache_ttl=5)

    def ticker(self, product: Product, market_id: Optional[str]) -> dict:
        # GetTickerInput has no marketId: the ticker is per leaf, across venues.
        inp = {"assetConfigurationId": product.asset_configuration_id, "keyId": product.key_id}
        return (self.client.execute(Q.GET_TICKER, {"input": inp}, cache_ttl=15) or {}).get("keyTicker") or {}

    def product_market_stats(self, product: Product, market_id: Optional[str], period_days: int = 30) -> dict:
        inp = {"assetConfigurationId": product.asset_configuration_id, "keyId": product.key_id, "periodDays": period_days}
        if market_id:
            inp["marketId"] = market_id
        return self.client.execute(Q.GET_PRODUCT_MARKET, {"input": inp}, cache_ttl=60) or {}

    def recent_trades(self, product: Product, market_id: Optional[str], limit: int = 50) -> list[dict]:
        # GetRecentTradesInput has no marketId: the tape is per leaf, across venues.
        inp = {"assetConfigurationId": product.asset_configuration_id, "keyId": product.key_id, "limit": limit}
        return (self.client.execute(Q.GET_RECENT_TRADES, {"input": inp}, cache_ttl=15) or {}).get("trades") or []

    def candles(self, product: Product, market_id: Optional[str], interval: str = "D1", limit: int = 30) -> list[dict]:
        # GetCandlesInput takes a time range, not a bar count.
        now = utcnow()
        span = {"M1": timedelta(minutes=limit), "M5": timedelta(minutes=5 * limit),
                "H1": timedelta(hours=limit)}.get(interval, timedelta(days=limit))
        inp = {"assetConfigurationId": product.asset_configuration_id, "keyId": product.key_id,
               "interval": interval, "fromTime": _iso(now - span), "toTime": _iso(now)}
        if market_id:
            inp["marketId"] = market_id
        return (self.client.execute(Q.GET_CANDLES, {"input": inp}, cache_ttl=300) or {}).get("candles") or []

    def fair_value(self, product: Product) -> tuple[Optional[int], Optional[str]]:
        """(valueCents, modelVersion). (None, None) means unpriced - NOT zero."""
        inp: dict[str, Any] = {"assetConfigurationId": product.asset_configuration_id}
        if product.key_id:
            inp["keyIds"] = [product.key_id]
        payload = self.client.execute(Q.GET_KEY_FAIR_VALUES, {"input": inp}, cache_ttl=300)
        for k in payload.get("keys") or []:
            if product.key_id and str(k.get("keyId")) != str(product.key_id):
                continue
            fvs = k.get("fairValues") or []
            if fvs:
                fv = fvs[-1] if isinstance(fvs, list) else fvs
                return _int(fv.get("valueCents")), fv.get("modelVersion")
        return None, None

    def order_quote(self, product: Product, market_id: str, side: str, price_cents: int, quantity: int) -> dict:
        """Read-only all-in quote for a hypothetical order. Never submits anything."""
        inp = {"assetConfigurationId": product.asset_configuration_id, "keyId": product.key_id,
               "marketId": market_id, "side": side, "quantity": quantity}
        if price_cents:
            inp["allInPrice"] = price_cents
        return (self.client.execute(Q.GET_ORDER_QUOTE, {"input": inp}, use_cache=False) or {}).get("quote") or {}

    # --- assemble a VenueQuote --------------------------------------------
    def venue_quote(self, product: Product, market: dict, *, with_candles: bool = True) -> VenueQuote:
        market_id = str(market["id"])
        name = market.get("displayName") or market_id
        book = self.order_book(product, market_id)
        summ = book.get("summary") or {}
        bids = [self._level(b) for b in book.get("bids") or []]
        asks = [self._level(a) for a in book.get("asks") or []]
        bids.sort(key=lambda l: -l.price_cents)
        asks.sort(key=lambda l: l.price_cents)

        q = VenueQuote(
            venue=f"TPX:{name}",
            product_id=product.asset_configuration_id,
            product_name=product.name,
            market_id=market_id,
            is_vaulted=market.get("isVaulted"),
            trading_state=market.get("tradingState"),
            best_bid=_int(summ.get("topBidCents")) if summ.get("topBidCents") is not None else (bids[0].price_cents if bids else None),
            best_ask=_int(summ.get("topAskCents")) if summ.get("topAskCents") is not None else (asks[0].price_cents if asks else None),
            bid_qty=bids[0].quantity if bids else 0,
            ask_qty=asks[0].quantity if asks else 0,
            bid_depth_units=_int(summ.get("bidUnits")) or sum(b.quantity for b in bids),
            ask_depth_units=_int(summ.get("askUnits")) or sum(a.quantity for a in asks),
            bid_depth_cents=_int(summ.get("bidDepthCents")),
            ask_depth_cents=_int(summ.get("askDepthCents")),
            imbalance_pct=_dec(summ.get("imbalancePercent")),
            makers_bid=bids[0].num_makers if bids else None,
            makers_ask=asks[0].num_makers if asks else None,
            top_bid_resting_since=bids[0].resting_since if bids else None,
            top_ask_resting_since=asks[0].resting_since if asks else None,
            bids=bids,
            asks=asks,
            fetched_at=_dt(book.get("serverNow")) or utcnow(),
            source="TPX getOrderBook",
            raw={"book_sequence": book.get("sequence"), "market": market},
        )

        try:
            t = self.ticker(product, market_id)
            q.last_trade_price = _int(t.get("lastPriceCents"))
            q.last_trade_at = _dt(t.get("lastTradeAt"))
            q.vol_24h = _int(t.get("volume24h"))
            q.trade_count_24h = _int(t.get("tradeCount24h"))
            q.high_24h = _int(t.get("high24hCents"))
            q.low_24h = _int(t.get("low24hCents"))
        except (TPXDomainError, TPXGraphQLError) as exc:
            log.info("ticker unavailable for %s: %s", name, exc)

        try:
            pm = self.product_market_stats(product, market_id, 30)
            st = pm.get("stats") or {}
            q.units_30d = _int(st.get("unitVolume"))
            q.trades_30d = _int(st.get("tradeCount"))
            q.vwap_30d = _int(st.get("vwapCents"))
            q.price_change_30d_pct = _dec(st.get("priceChangePercent"))
            q.holder_count = _int(st.get("holderCount"))
            if q.last_trade_at is None:
                q.last_trade_at = _dt(st.get("lastTradeAt"))
            q.raw["price_series"] = pm.get("priceSeries")
        except (TPXDomainError, TPXGraphQLError) as exc:
            log.info("product market stats unavailable for %s: %s", name, exc)

        if with_candles:
            try:
                bars = self.candles(product, market_id, "D1", 30)
                q.raw["candles_d1"] = daily_closes(bars)
                q.candles_30d = summarise_candles(bars, "D1")
                q.candles_7d = summarise_candles(bars[-7:], "D1")
            except (TPXDomainError, TPXGraphQLError) as exc:
                log.info("candles unavailable for %s: %s", name, exc)
            try:  # hourly bars for the last 7 days: the intraday view of the detail chart
                q.raw["candles_h1"] = daily_closes(self.candles(product, market_id, "H1", 7 * 24), 16)
            except (TPXDomainError, TPXGraphQLError) as exc:
                log.info("hourly candles unavailable for %s: %s", name, exc)
            try:  # the tape: last trades, newest first
                q.raw["tape"] = [{"t": str(t.get("executedAt") or "")[:19], "p": _int(t.get("priceCents")),
                                  "q": _int(t.get("quantity")) or 0, "s": (t.get("takerSide") or "").upper()[:1]}
                                 for t in self.recent_trades(product, market_id, 40) if t.get("priceCents")]
            except (TPXDomainError, TPXGraphQLError) as exc:
                log.info("recent trades unavailable for %s: %s", name, exc)

        return q

    @staticmethod
    def _level(l: dict) -> BookLevel:
        return BookLevel(
            price_cents=_int(l.get("priceCents")) or 0,
            quantity=_int(l.get("quantity")) or 0,
            num_makers=_int(l.get("numMakers")),
            resting_since=_dt(l.get("restingSince")),
            last_changed_at=_dt(l.get("lastChangedAt")),
        )


def summarise_candles(bars: list[dict], interval: str) -> Optional[CandleSummary]:
    bars = [b for b in bars if b]
    if not bars:
        return None
    bars = sorted(bars, key=_bar_time)
    highs = [_int(b.get("highCents")) for b in bars if _int(b.get("highCents")) is not None]
    lows = [_int(b.get("lowCents")) for b in bars if _int(b.get("lowCents")) is not None]
    vols = [(_int(b.get("volume")) or 0, _int(b.get("vwapCents"))) for b in bars]
    total_vol = sum(v for v, _ in vols)
    vwap = None
    if total_vol:
        num = sum(v * (p or 0) for v, p in vols if p is not None)
        vwap = int(num / total_vol) if num else None
    return CandleSummary(
        interval=interval,
        bars=len(bars),
        open_cents=_int(bars[0].get("openCents")),
        close_cents=_int(bars[-1].get("closeCents")),
        high_cents=max(highs) if highs else None,
        low_cents=min(lows) if lows else None,
        volume=total_vol,
        vwap_cents=vwap,
    )


def daily_closes(bars: list[dict], time_chars: int = 10) -> list[dict]:
    """Compact OHLC series for charts: [{t, open, high, low, close, vwap, volume, trades}]
    oldest first. `time_chars` keeps the date (10) for daily bars or date+hour (16) for intraday."""
    out = []
    for b in sorted((b for b in bars if b), key=_bar_time):
        close = _int(b.get("closeCents"))
        if close is None:
            continue
        out.append({"t": _bar_time(b)[:time_chars], "open": _int(b.get("openCents")), "high": _int(b.get("highCents")),
                    "low": _int(b.get("lowCents")), "close": close, "vwap": _int(b.get("vwapCents")),
                    "volume": _int(b.get("volume")) or 0, "trades": _int(b.get("trades")) or 0})
    return out
