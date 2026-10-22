"""Pure edge math. Integer/Decimal cents in, Decimal cents out. No I/O.

Conventions
-----------
* Book prices (VenueQuote.bids/asks, best_bid/best_ask) are CLEAN (pre-fee).
* CostModel converts clean -> all-in and adds shipping per physical move.
* "edge" is proceeds - cost per unit; "pct" is edge / capital deployed (cost).
* Margin M is on capital deployed: proceeds >= cost * (1 + M).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Literal, Optional, Sequence

from .models import BookLevel, CostModel, VenueQuote, utcnow

ZERO = Decimal(0)
DEFAULT_SIZES: tuple[int, ...] = (1, 3, 5, 10)


# ---------------------------------------------------------------------------
# Taker arbitrage with book walking
# ---------------------------------------------------------------------------
@dataclass
class Fill:
    size: int  # requested
    filled: int  # achievable from both books
    cost_cents: Decimal  # total all-in cost incl. shipping_in
    proceeds_cents: Decimal  # total all-in proceeds net of shipping_out

    @property
    def edge_cents(self) -> Decimal:
        return self.proceeds_cents - self.cost_cents

    @property
    def edge_per_unit_cents(self) -> Optional[Decimal]:
        return self.edge_cents / self.filled if self.filled else None

    @property
    def edge_pct(self) -> Optional[Decimal]:
        return self.edge_cents / self.cost_cents * 100 if self.cost_cents else None


@dataclass
class TakerArb:
    label: str  # e.g. "A: buy Dealernet -> sell TPX:Vault"
    buy_venue: str
    sell_venue: str
    buy_clean_cents: Optional[int]  # best ask hit
    sell_clean_cents: Optional[int]  # best bid hit
    unit_cost_cents: Optional[Decimal]  # all-in incl shipping, at top of book
    unit_proceeds_cents: Optional[Decimal]
    max_size: int
    fills: list[Fill] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def edge_per_unit_cents(self) -> Optional[Decimal]:
        if self.unit_cost_cents is None or self.unit_proceeds_cents is None:
            return None
        return self.unit_proceeds_cents - self.unit_cost_cents

    @property
    def edge_pct(self) -> Optional[Decimal]:
        e = self.edge_per_unit_cents
        if e is None or not self.unit_cost_cents:
            return None
        return e / self.unit_cost_cents * 100

    @property
    def is_positive(self) -> bool:
        e = self.edge_per_unit_cents
        return e is not None and e > 0


def _take(levels: Sequence[BookLevel], n: int) -> list[tuple[int, int]]:
    """Return [(price, qty)] consuming up to n units from the front of `levels`."""
    out: list[tuple[int, int]] = []
    left = n
    for lv in levels:
        if left <= 0:
            break
        q = min(lv.quantity, left)
        if q > 0:
            out.append((lv.price_cents, q))
            left -= q
    return out


def walk_fill(size: int, asks: Sequence[BookLevel], buy_cm: CostModel,
              bids: Sequence[BookLevel], sell_cm: CostModel) -> Fill:
    """Cost of buying `size` units from `asks` and selling them into `bids`, both as taker."""
    buys = _take(asks, size)
    sells = _take(bids, size)
    filled = min(sum(q for _, q in buys), sum(q for _, q in sells))
    # Re-take exactly `filled` on each side so both legs match.
    buys = _take(asks, filled)
    sells = _take(bids, filled)
    cost = sum((buy_cm.buy_cost_cents(p, "taker") * q for p, q in buys), ZERO)
    proceeds = sum((sell_cm.sell_proceeds_cents(p, "taker") * q for p, q in sells), ZERO)
    return Fill(size=size, filled=filled, cost_cents=cost, proceeds_cents=proceeds)


def taker_arb(buy: VenueQuote, buy_cm: CostModel, sell: VenueQuote, sell_cm: CostModel,
              label: str, sizes: Sequence[int] = DEFAULT_SIZES) -> TakerArb:
    """Buy at `buy`'s asks (taker), sell into `sell`'s bids (taker)."""
    asks = buy.asks or ([BookLevel(price_cents=buy.best_ask, quantity=buy.ask_qty or 1)] if buy.best_ask else [])
    bids = sell.bids or ([BookLevel(price_cents=sell.best_bid, quantity=sell.bid_qty or 1)] if sell.best_bid else [])
    arb = TakerArb(
        label=label,
        buy_venue=buy.venue,
        sell_venue=sell.venue,
        buy_clean_cents=asks[0].price_cents if asks else None,
        sell_clean_cents=bids[0].price_cents if bids else None,
        unit_cost_cents=buy_cm.buy_cost_cents(asks[0].price_cents, "taker") if asks else None,
        unit_proceeds_cents=sell_cm.sell_proceeds_cents(bids[0].price_cents, "taker") if bids else None,
        max_size=min(sum(a.quantity for a in asks), sum(b.quantity for b in bids)) if asks and bids else 0,
    )
    if not asks:
        arb.notes.append(f"no asks on {buy.venue}")
    if not bids:
        arb.notes.append(f"no bids on {sell.venue}")
    if asks and bids:
        for s in sizes:
            arb.fills.append(walk_fill(s, asks, buy_cm, bids, sell_cm))
    if sell.is_vaulted:
        arb.notes.append("sell leg is a vaulted market: units must already be in the vault (deposit shipping not modelled)")
    if buy.is_vaulted:
        arb.notes.append("buy leg is a vaulted market: withdraw to ship (withdrawal shipping not modelled)")
    return arb


# ---------------------------------------------------------------------------
# Maker edges
# ---------------------------------------------------------------------------
@dataclass
class MakerLevel:
    label: str  # e.g. "rest BID on TPX:Vault, flip into Dealernet bid"
    venue: str  # where I rest
    side: Literal["bid", "ask"]
    rest_clean_cents: Optional[int]  # the price to rest (clean, tick-rounded)
    counter_venue: str
    counter_clean_cents: Optional[int]  # the other venue's price I'd flip against
    current_top_cents: Optional[int]  # current best on the resting side of my venue
    would_be_best: Optional[bool]  # resting here is at/inside the current best
    improves_by_ticks: Optional[int]  # how many ticks inside current best (negative = behind)
    expected_edge_cents: Optional[Decimal]  # per unit, at the rounded price
    expected_edge_pct: Optional[Decimal]
    notes: list[str] = field(default_factory=list)


def highest_bid_to_flip(rest_cm: CostModel, rest_quote: VenueQuote, flip_quote: VenueQuote,
                        flip_cm: CostModel, margin: Decimal, label: str) -> MakerLevel:
    """Highest clean bid I can rest on `rest` (maker) that still nets >= margin
    when I flip the unit into `flip`'s best bid (taker)."""
    ml = MakerLevel(label=label, venue=rest_quote.venue, side="bid", rest_clean_cents=None,
                    counter_venue=flip_quote.venue, counter_clean_cents=flip_quote.best_bid,
                    current_top_cents=rest_quote.best_bid, would_be_best=None, improves_by_ticks=None,
                    expected_edge_cents=None, expected_edge_pct=None)
    if flip_quote.best_bid is None:
        ml.notes.append(f"no bid on {flip_quote.venue} to flip into")
        return ml
    proceeds = flip_cm.sell_proceeds_cents(flip_quote.best_bid, "taker")
    fb = Decimal(rest_cm.fee_bps("buy", "maker")) / 10_000
    # cost(p) = p*(1+fb) + fixed + ship_in  <=  proceeds / (1+M)
    max_cost = proceeds / (1 + margin)
    p = (max_cost - rest_cm.fixed_fee_cents - rest_cm.shipping_in_cents) / (1 + fb)
    if p <= 0:
        ml.notes.append("no positive bid clears the target margin")
        return ml
    rest = rest_cm.round_down_to_tick(p)
    ml.rest_clean_cents = rest
    cost = rest_cm.buy_cost_cents(rest, "maker")
    ml.expected_edge_cents = proceeds - cost
    ml.expected_edge_pct = (proceeds - cost) / cost * 100 if cost else None
    _best_flags(ml, rest, rest_quote.best_bid, rest_cm.tick_size_cents, side="bid")
    return ml


def lowest_ask_to_cover(rest_cm: CostModel, rest_quote: VenueQuote, cover_quote: VenueQuote,
                        cover_cm: CostModel, margin: Decimal, label: str) -> MakerLevel:
    """Lowest clean ask I can rest on `rest` (maker) that still nets >= margin
    if I source the unit by hitting `cover`'s best ask (taker)."""
    ml = MakerLevel(label=label, venue=rest_quote.venue, side="ask", rest_clean_cents=None,
                    counter_venue=cover_quote.venue, counter_clean_cents=cover_quote.best_ask,
                    current_top_cents=rest_quote.best_ask, would_be_best=None, improves_by_ticks=None,
                    expected_edge_cents=None, expected_edge_pct=None)
    if cover_quote.best_ask is None:
        ml.notes.append(f"no ask on {cover_quote.venue} to source from")
        return ml
    cost = cover_cm.buy_cost_cents(cover_quote.best_ask, "taker")
    fs = Decimal(rest_cm.fee_bps("sell", "maker")) / 10_000
    if fs >= 1:
        ml.notes.append("seller fee >= 100%?")
        return ml
    # proceeds(p) = p*(1-fs) - fixed - ship_out  >=  cost*(1+M)
    p = (cost * (1 + margin) + rest_cm.fixed_fee_cents + rest_cm.shipping_out_cents) / (1 - fs)
    rest = rest_cm.round_up_to_tick(p)
    ml.rest_clean_cents = rest
    proceeds = rest_cm.sell_proceeds_cents(rest, "maker")
    ml.expected_edge_cents = proceeds - cost
    ml.expected_edge_pct = (proceeds - cost) / cost * 100 if cost else None
    _best_flags(ml, rest, rest_quote.best_ask, rest_cm.tick_size_cents, side="ask")
    return ml


def _best_flags(ml: MakerLevel, rest: int, current: Optional[int], tick: int, side: str) -> None:
    tick = tick or 1
    if current is None:
        ml.would_be_best = True
        ml.improves_by_ticks = None
        ml.notes.append("book side is empty: you would be alone at the top")
        return
    diff = (rest - current) if side == "bid" else (current - rest)
    ml.improves_by_ticks = int(Decimal(diff) / tick)
    ml.would_be_best = diff >= tick
    if diff == 0:
        ml.notes.append("ties the current best (queue priority behind existing makers)")


# ---------------------------------------------------------------------------
# Fair value gaps
# ---------------------------------------------------------------------------
@dataclass
class FairValueGaps:
    fair_value_cents: Optional[int]
    model_version: Optional[str]
    dn_bid_gap_pct: Optional[Decimal]  # DN_bid / FV - 1
    dn_ask_gap_pct: Optional[Decimal]
    tpx_mid_gap_pct: Optional[Decimal]
    tpx_bid_gap_pct: Optional[Decimal]
    tpx_ask_gap_pct: Optional[Decimal]
    flags: list[str] = field(default_factory=list)

    @property
    def has_fv(self) -> bool:
        return self.fair_value_cents is not None


def _gap(price: Optional[Decimal | int], fv: Optional[int]) -> Optional[Decimal]:
    if price is None or not fv:
        return None
    return (Decimal(price) / Decimal(fv) - 1) * 100


def fair_value_gaps(tpx: VenueQuote, dn: Optional[VenueQuote]) -> FairValueGaps:
    fv = tpx.fair_value
    g = FairValueGaps(
        fair_value_cents=fv,
        model_version=tpx.fair_value_model,
        dn_bid_gap_pct=_gap(dn.best_bid, fv) if dn else None,
        dn_ask_gap_pct=_gap(dn.best_ask, fv) if dn else None,
        tpx_mid_gap_pct=_gap(tpx.mid, fv),
        tpx_bid_gap_pct=_gap(tpx.best_bid, fv),
        tpx_ask_gap_pct=_gap(tpx.best_ask, fv),
    )
    if fv is None:
        g.flags.append("no FV")
        return g
    if g.dn_ask_gap_pct is not None and g.dn_ask_gap_pct < 0:
        g.flags.append("Dealernet ask BELOW fair value")
    if g.dn_bid_gap_pct is not None and g.dn_bid_gap_pct > 0:
        g.flags.append("Dealernet bid ABOVE fair value")
    if g.tpx_ask_gap_pct is not None and g.tpx_ask_gap_pct < 0:
        g.flags.append("TPX ask below fair value")
    if g.tpx_bid_gap_pct is not None and g.tpx_bid_gap_pct > 0:
        g.flags.append("TPX bid above fair value")
    return g


# ---------------------------------------------------------------------------
# Spread, liquidity, staleness
# ---------------------------------------------------------------------------
@dataclass
class Liquidity:
    venue: str
    spread_pct: Optional[Decimal]
    bid_units: int
    ask_units: int
    imbalance_pct: Optional[Decimal]
    top_bid_age_days: Optional[Decimal]
    top_ask_age_days: Optional[Decimal]
    units_30d: Optional[int]
    trades_30d: Optional[int]
    holder_count: Optional[int]
    flags: list[str] = field(default_factory=list)
    factor: Decimal = Decimal(1)  # 0..1 used for ranking


def liquidity(q: VenueQuote, stale_days: int, thin_units: int, size_cap: int,
              now: Optional[datetime] = None) -> Liquidity:
    now = now or utcnow()
    li = Liquidity(
        venue=q.venue,
        spread_pct=q.spread_pct,
        bid_units=q.bid_depth_units,
        ask_units=q.ask_depth_units,
        imbalance_pct=q.imbalance_pct,
        top_bid_age_days=q.top_age_days("bid", now),
        top_ask_age_days=q.top_age_days("ask", now),
        units_30d=q.units_30d,
        trades_30d=q.trades_30d,
        holder_count=q.holder_count,
    )
    stale = False
    for side, age in (("bid", li.top_bid_age_days), ("ask", li.top_ask_age_days)):
        if age is not None and age > stale_days:
            li.flags.append(f"stale {side} ({age:.0f}d)")
            stale = True
    if q.last_trade_at is not None and (now - q.last_trade_at).days > stale_days:
        li.flags.append(f"no trade in {(now - q.last_trade_at).days}d")
    if 0 < li.bid_units < thin_units:
        li.flags.append("thin bids")
    if 0 < li.ask_units < thin_units:
        li.flags.append("thin asks")
    if li.bid_units == 0:
        li.flags.append("no bids")
    if li.ask_units == 0:
        li.flags.append("no asks")
    if li.spread_pct is not None and li.spread_pct > 15:
        li.flags.append(f"wide spread {li.spread_pct:.1f}%")

    cap = Decimal(max(size_cap, 1))
    depth_part = min(Decimal(1), Decimal(min(li.bid_units, li.ask_units)) / cap)
    if q.units_30d is None:
        vel_part = Decimal("0.5")  # unknown velocity (e.g. Dealernet): neutral
    else:
        vel_part = min(Decimal(1), Decimal(q.units_30d) / cap)
    f = depth_part * Decimal("0.4") + vel_part * Decimal("0.6")
    if stale:
        f *= Decimal("0.5")
    li.factor = f.quantize(Decimal("0.001"))
    return li


# ---------------------------------------------------------------------------
# Per-(product, TPX market) assessment + ranking
# ---------------------------------------------------------------------------
@dataclass
class Assessment:
    product_name: str
    tpx_market: str
    tpx_is_vaulted: Optional[bool]
    tpx: VenueQuote
    dn: Optional[VenueQuote]
    arb_a: Optional[TakerArb]  # buy Dealernet -> sell TPX
    arb_b: Optional[TakerArb]  # buy TPX -> sell Dealernet
    makers: list[MakerLevel]
    fv: FairValueGaps
    tpx_liq: Liquidity
    dn_liq: Optional[Liquidity]
    size_cap: int

    @property
    def best_arb(self) -> Optional[TakerArb]:
        cands = [a for a in (self.arb_a, self.arb_b) if a and a.edge_per_unit_cents is not None]
        return max(cands, key=lambda a: a.edge_per_unit_cents) if cands else None

    @property
    def risk_adjusted_score(self) -> Decimal:
        """edge $ x min(size, cap) x liquidity factor (TPX x Dealernet), in dollars.

        The liquidity factor (0..1) only scales *positive* edges down; a negative
        edge is reported as edge $ x size so an illiquid book never looks "less
        bad" than a liquid one with the same loss.
        """
        a = self.best_arb
        if a is None or a.edge_per_unit_cents is None:
            return ZERO
        size = Decimal(min(a.max_size, self.size_cap))
        f = self.tpx_liq.factor * (self.dn_liq.factor if self.dn_liq else Decimal(1))
        if a.edge_per_unit_cents <= 0:
            f = Decimal(1)
        return (a.edge_per_unit_cents * size * f / 100).quantize(Decimal("0.01"))


def assess(tpx: VenueQuote, tpx_cm: CostModel, dn: Optional[VenueQuote], dn_cm: Optional[CostModel],
           *, margin: Decimal, stale_days: int, thin_units: int, size_cap: int,
           sizes: Sequence[int] = DEFAULT_SIZES, now: Optional[datetime] = None) -> Assessment:
    now = now or utcnow()
    arb_a = arb_b = None
    makers: list[MakerLevel] = []
    if dn is not None and dn_cm is not None:
        arb_a = taker_arb(dn, dn_cm, tpx, tpx_cm, f"A: buy Dealernet (taker) -> sell {tpx.venue} (taker)", sizes)
        arb_b = taker_arb(tpx, tpx_cm, dn, dn_cm, f"B: buy {tpx.venue} (taker) -> sell Dealernet (taker)", sizes)
        makers = [
            highest_bid_to_flip(tpx_cm, tpx, dn, dn_cm, margin, f"rest BID on {tpx.venue} (maker), flip into Dealernet bid (taker)"),
            highest_bid_to_flip(dn_cm, dn, tpx, tpx_cm, margin, f"rest BID on Dealernet (maker, 3%), flip into {tpx.venue} bid (taker)"),
            lowest_ask_to_cover(tpx_cm, tpx, dn, dn_cm, margin, f"rest ASK on {tpx.venue} (maker), cover from Dealernet ask (taker)"),
            lowest_ask_to_cover(dn_cm, dn, tpx, tpx_cm, margin, f"rest ASK on Dealernet (maker, 3%), cover from {tpx.venue} ask (taker)"),
        ]
    return Assessment(
        product_name=tpx.product_name,
        tpx_market=tpx.venue,
        tpx_is_vaulted=tpx.is_vaulted,
        tpx=tpx,
        dn=dn,
        arb_a=arb_a,
        arb_b=arb_b,
        makers=makers,
        fv=fair_value_gaps(tpx, dn),
        tpx_liq=liquidity(tpx, stale_days, thin_units, size_cap, now),
        dn_liq=liquidity(dn, stale_days, thin_units, size_cap, now) if dn else None,
        size_cap=size_cap,
    )


def rank(assessments: Sequence[Assessment]) -> list[Assessment]:
    """Best score first; rows with no computable arb (no Dealernet data) sink to the bottom."""
    return sorted(assessments, key=lambda a: (a.best_arb is not None, a.risk_adjusted_score), reverse=True)
