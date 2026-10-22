"""Shared data model. All prices are integer cents. Clean vs all-in is always
labelled: VenueQuote holds CLEAN (pre-fee) prices; CostModel converts."""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, ROUND_FLOOR, ROUND_CEILING
from enum import Enum
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field

Side = Literal["bid", "ask"]
Role = Literal["maker", "taker"]


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class BookLevel(BaseModel):
    price_cents: int  # CLEAN
    quantity: int
    num_makers: Optional[int] = None
    resting_since: Optional[datetime] = None
    last_changed_at: Optional[datetime] = None


class FeeModelKind(str, Enum):
    BUYER_SELLER = "BUYER_SELLER"
    MAKER_TAKER = "MAKER_TAKER"


class CostModel(BaseModel):
    """Per-venue fee + friction schedule.

    BUYER_SELLER: buyer pays buyer_fee_bps, seller pays seller_fee_bps.
    MAKER_TAKER: the resting side pays maker_fee_bps, the aggressing side pays
    taker_fee_bps regardless of buy/sell.
    """

    venue: str
    kind: FeeModelKind = FeeModelKind.BUYER_SELLER
    buyer_fee_bps: int = 0
    seller_fee_bps: int = 0
    maker_fee_bps: int = 0
    taker_fee_bps: int = 0
    fixed_fee_cents: int = 0
    shipping_in_cents: int = 0  # cost to get a unit from this venue to me
    shipping_out_cents: int = 0  # cost to deliver a unit I sell on this venue
    settlement_days: Optional[int] = None
    tick_size_cents: int = 1
    lot_size: int = 1
    source: str = "config"

    def fee_bps(self, side: Literal["buy", "sell"], role: Role) -> int:
        if self.kind == FeeModelKind.MAKER_TAKER:
            return self.taker_fee_bps if role == "taker" else self.maker_fee_bps
        return self.buyer_fee_bps if side == "buy" else self.seller_fee_bps

    # --- clean <-> all-in -------------------------------------------------
    def all_in_buy_cents(self, clean_cents: int, role: Role) -> Decimal:
        """What I pay per unit to BUY at a clean price (fees only, no shipping)."""
        bps = Decimal(self.fee_bps("buy", role))
        return Decimal(clean_cents) * (1 + bps / 10_000) + self.fixed_fee_cents

    def all_in_sell_cents(self, clean_cents: int, role: Role) -> Decimal:
        """What I receive per unit to SELL at a clean price (fees only, no shipping)."""
        bps = Decimal(self.fee_bps("sell", role))
        return Decimal(clean_cents) * (1 - bps / 10_000) - self.fixed_fee_cents

    def buy_cost_cents(self, clean_cents: int, role: Role) -> Decimal:
        """All-in buy price plus the shipping needed to receive the unit."""
        return self.all_in_buy_cents(clean_cents, role) + self.shipping_in_cents

    def sell_proceeds_cents(self, clean_cents: int, role: Role) -> Decimal:
        """All-in sell proceeds minus the shipping needed to deliver the unit."""
        return self.all_in_sell_cents(clean_cents, role) - self.shipping_out_cents

    def round_down_to_tick(self, cents: Decimal) -> int:
        t = Decimal(self.tick_size_cents or 1)
        return int((cents / t).to_integral_value(rounding=ROUND_FLOOR) * t)

    def round_up_to_tick(self, cents: Decimal) -> int:
        t = Decimal(self.tick_size_cents or 1)
        return int((cents / t).to_integral_value(rounding=ROUND_CEILING) * t)


class VenueQuote(BaseModel):
    venue: str  # "TPX:<marketDisplayName>" or "Dealernet"
    product_id: str
    product_name: str = ""
    market_id: Optional[str] = None
    is_vaulted: Optional[bool] = None
    trading_state: Optional[str] = None

    best_bid: Optional[int] = None  # CLEAN cents
    best_ask: Optional[int] = None  # CLEAN cents
    bid_qty: int = 0
    ask_qty: int = 0
    bid_depth_units: int = 0
    ask_depth_units: int = 0
    bid_depth_cents: Optional[int] = None
    ask_depth_cents: Optional[int] = None
    imbalance_pct: Optional[Decimal] = None
    makers_bid: Optional[int] = None
    makers_ask: Optional[int] = None
    top_bid_resting_since: Optional[datetime] = None
    top_ask_resting_since: Optional[datetime] = None
    bids: list[BookLevel] = Field(default_factory=list)
    asks: list[BookLevel] = Field(default_factory=list)

    last_trade_price: Optional[int] = None
    last_trade_at: Optional[datetime] = None
    vol_24h: Optional[int] = None
    trade_count_24h: Optional[int] = None
    high_24h: Optional[int] = None
    low_24h: Optional[int] = None
    units_30d: Optional[int] = None
    trades_30d: Optional[int] = None
    vwap_30d: Optional[int] = None
    price_change_30d_pct: Optional[Decimal] = None
    holder_count: Optional[int] = None
    fair_value: Optional[int] = None  # TPX only; None == unpriced ("no FV")
    fair_value_model: Optional[str] = None
    candles_7d: Optional["CandleSummary"] = None
    candles_30d: Optional["CandleSummary"] = None
    # TCGplayer reference price for the same product (sealed TCG only); cents.
    tcg_product_id: Optional[str] = None
    tcg_market: Optional[int] = None
    tcg_low: Optional[int] = None
    tcg_last: Optional[int] = None  # most recent TCGplayer sale (page source)
    tcg_last_at: Optional[str] = None
    tcg_trend: Optional[list] = None  # [{t, market, sold}]
    tcg_trend_pct: Optional[float] = None

    fetched_at: datetime = Field(default_factory=utcnow)
    source: str = ""
    raw: dict[str, Any] = Field(default_factory=dict)

    @property
    def mid(self) -> Optional[Decimal]:
        if self.best_bid is None or self.best_ask is None:
            return None
        return (Decimal(self.best_bid) + Decimal(self.best_ask)) / 2

    @property
    def spread_pct(self) -> Optional[Decimal]:
        m = self.mid
        if m is None or m == 0:
            return None
        return (Decimal(self.best_ask) - Decimal(self.best_bid)) / m * 100

    def top_age_days(self, side: Side, now: Optional[datetime] = None) -> Optional[Decimal]:
        ts = self.top_bid_resting_since if side == "bid" else self.top_ask_resting_since
        if ts is None:
            return None
        now = now or utcnow()
        return Decimal((now - ts).total_seconds()) / Decimal(86_400)


class CandleSummary(BaseModel):
    interval: str
    bars: int
    open_cents: Optional[int] = None
    close_cents: Optional[int] = None
    high_cents: Optional[int] = None
    low_cents: Optional[int] = None
    volume: int = 0
    vwap_cents: Optional[int] = None

    @property
    def change_pct(self) -> Optional[Decimal]:
        if not self.open_cents or self.close_cents is None:
            return None
        return (Decimal(self.close_cents) - Decimal(self.open_cents)) / Decimal(self.open_cents) * 100


VenueQuote.model_rebuild()


class Product(BaseModel):
    """A TPX asset configuration (the product) + the key we price (MINT)."""

    asset_configuration_id: str
    name: str
    asset_type_id: Optional[str] = None
    asset_type_name: Optional[str] = None
    key_id: Optional[str] = None
    key_label: str = "MINT"
    format: Optional[str] = None  # hobby/jumbo/blaster/case/...
    is_case: bool = False
    year: Optional[int] = None
    manufacturer: Optional[str] = None
    brand: Optional[str] = None
    sport: Optional[str] = None
    raw: dict[str, Any] = Field(default_factory=dict)
