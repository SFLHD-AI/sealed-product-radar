"""Dealernet source interface.

Dealernet (dealernetx.com) has no public API. A product page shows buy offers
(bids) on the left and sell offers (asks) on the right, each with a member ID
and quantity. We model that as DealernetQuote with full level lists and expose
three backends: manual (flags / prompt), csv (file or stdin paste) and scrape
(not built until DEALERNET_ACCESS=scrape is confirmed).
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field

from ..models import BookLevel, CostModel, FeeModelKind, VenueQuote, utcnow

VENUE = "Dealernet"


class DealernetLevel(BaseModel):
    price_cents: int  # listed price (clean: Dealernet fees apply only to name-your-price)
    quantity: int = 1
    member: Optional[str] = None
    observed_at: Optional[datetime] = None


class DealernetQuote(BaseModel):
    product: str  # the Dealernet product name as shown / as typed
    bids: list[DealernetLevel] = Field(default_factory=list)
    asks: list[DealernetLevel] = Field(default_factory=list)
    # Case-format offers on a single-unit product, priced per unit. Cases trade at
    # a premium and need a full case to fill, so they never mix into bids/asks.
    case_bids: list[DealernetLevel] = Field(default_factory=list)
    case_asks: list[DealernetLevel] = Field(default_factory=list)
    boxes_per_case: Optional[int] = None
    fetched_at: datetime = Field(default_factory=utcnow)
    source: str = "manual"
    url: Optional[str] = None
    upc: Optional[str] = None  # the UPC printed on the Dealernet listing
    upc_match: Optional[bool] = None  # True = same UPC as Tradepost, False = differs, None = unknown
    upc_note: Optional[str] = None  # plain-words explanation when the UPC differs

    def to_venue_quote(self, product_id: str, product_name: str = "") -> VenueQuote:
        bids = sorted(self.bids, key=lambda l: -l.price_cents)
        asks = sorted(self.asks, key=lambda l: l.price_cents)
        return VenueQuote(
            venue=VENUE,
            product_id=product_id,
            product_name=product_name or self.product,
            best_bid=bids[0].price_cents if bids else None,
            best_ask=asks[0].price_cents if asks else None,
            bid_qty=bids[0].quantity if bids else 0,
            ask_qty=asks[0].quantity if asks else 0,
            bid_depth_units=sum(b.quantity for b in bids),
            ask_depth_units=sum(a.quantity for a in asks),
            bid_depth_cents=sum(b.price_cents * b.quantity for b in bids) or None,
            ask_depth_cents=sum(a.price_cents * a.quantity for a in asks) or None,
            makers_bid=len({b.member for b in bids if b.member}) or (len(bids) or None),
            makers_ask=len({a.member for a in asks if a.member}) or (len(asks) or None),
            top_bid_resting_since=bids[0].observed_at if bids else None,
            top_ask_resting_since=asks[0].observed_at if asks else None,
            bids=[BookLevel(price_cents=b.price_cents, quantity=b.quantity, resting_since=b.observed_at) for b in bids],
            asks=[BookLevel(price_cents=a.price_cents, quantity=a.quantity, resting_since=a.observed_at) for a in asks],
            fetched_at=self.fetched_at,
            source=f"Dealernet ({self.source})",
            raw={"url": self.url, "product": self.product, "boxes_per_case": self.boxes_per_case,
                 "case_bid": max((l.price_cents for l in self.case_bids), default=None),
                 "case_ask": min((l.price_cents for l in self.case_asks), default=None),
                 "case_bid_qty": sum(l.quantity for l in self.case_bids),
                 "case_ask_qty": sum(l.quantity for l in self.case_asks),
                 "upc": self.upc, "upc_match": self.upc_match, "upc_note": self.upc_note},
        )


def dealernet_cost_model(plan: str, listed_fee_bps: int, named_price_fee_bps: int,
                         shipping_in_cents: int, shipping_out_cents: int) -> CostModel:
    """Dealernet fees depend on *how* you trade, not which side: hitting a
    listed price (taker) is free; naming your own price / countering (maker)
    costs 3% on the Vendor plan. That is exactly a MAKER_TAKER schedule."""
    return CostModel(
        venue=VENUE,
        kind=FeeModelKind.MAKER_TAKER,
        maker_fee_bps=named_price_fee_bps,
        taker_fee_bps=listed_fee_bps,
        shipping_in_cents=shipping_in_cents,
        shipping_out_cents=shipping_out_cents,
        settlement_days=7,  # 3-7 business days typical
        tick_size_cents=100,  # Dealernet prices are whole dollars
        source=f"Dealernet {plan} plan",
    )


class DealernetSource(ABC):
    name = "base"

    @abstractmethod
    def get_quote(self, product: str, upc: Optional[str] = None) -> Optional[DealernetQuote]:
        """Return the current bid/ask ladder for `product`, or None if unknown.
        `upc` is Tradepost's UPC for the product; a source that knows Dealernet's UPC
        uses it to confirm both sites mean the same box."""


def dollars_to_cents(v) -> int:
    from decimal import Decimal

    s = str(v).strip().replace("$", "").replace(",", "")
    return int((Decimal(s) * 100).quantize(Decimal("1")))
