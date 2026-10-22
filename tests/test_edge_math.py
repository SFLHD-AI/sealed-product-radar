"""Hand-checked edge math. Numbers mirror the README worked example."""
from decimal import Decimal

import pytest

from edge.edge import (assess, fair_value_gaps, highest_bid_to_flip, liquidity, lowest_ask_to_cover, taker_arb,
                       walk_fill)
from edge.models import BookLevel, CostModel, FeeModelKind, VenueQuote
from edge.dealernet.base import dealernet_cost_model


def tpx_vault_cm(ship=0):
    # Vault: BUYER_SELLER, buyer 0 bps, seller 500 bps, $1 tick, no shipping
    return CostModel(venue="TPX:Vault", kind=FeeModelKind.BUYER_SELLER, buyer_fee_bps=0, seller_fee_bps=500,
                     tick_size_cents=100, shipping_in_cents=ship, shipping_out_cents=ship)


def tpx_ship_cm():
    # Ship: MAKER_TAKER maker 100 / taker 400, $15 shipping each way
    return CostModel(venue="TPX:Ship", kind=FeeModelKind.MAKER_TAKER, maker_fee_bps=100, taker_fee_bps=400,
                     tick_size_cents=100, shipping_in_cents=1500, shipping_out_cents=1500)


def dn_cm():
    return dealernet_cost_model("vendor", 0, 300, 1500, 1500)


def quote(venue, bids, asks, **kw):
    b = [BookLevel(price_cents=p, quantity=q) for p, q in bids]
    a = [BookLevel(price_cents=p, quantity=q) for p, q in asks]
    return VenueQuote(venue=venue, product_id="x", best_bid=b[0].price_cents if b else None,
                      best_ask=a[0].price_cents if a else None, bid_qty=b[0].quantity if b else 0,
                      ask_qty=a[0].quantity if a else 0, bid_depth_units=sum(q for _, q in bids),
                      ask_depth_units=sum(q for _, q in asks), bids=b, asks=a, **kw)


# --- clean <-> all-in --------------------------------------------------------
def test_buyer_seller_fee_conversion():
    cm = tpx_vault_cm()
    assert cm.all_in_buy_cents(25800, "taker") == Decimal(25800)  # buyer pays 0
    assert cm.all_in_sell_cents(25800, "taker") == Decimal("24510")  # 25800 * 0.95


def test_maker_taker_fee_conversion():
    cm = tpx_ship_cm()
    assert cm.fee_bps("buy", "taker") == 400 and cm.fee_bps("buy", "maker") == 100
    assert cm.all_in_buy_cents(10000, "taker") == Decimal(10400)
    assert cm.all_in_sell_cents(10000, "maker") == Decimal(9900)
    assert cm.buy_cost_cents(10000, "taker") == Decimal(11900)  # + $15 shipping in


def test_dealernet_fee_is_zero_taker_three_percent_maker():
    cm = dn_cm()
    assert cm.all_in_buy_cents(25800, "taker") == Decimal(25800)
    assert cm.all_in_buy_cents(25800, "maker") == Decimal("26574")  # +3%
    assert cm.all_in_sell_cents(25200, "maker") == Decimal("24444")  # -3%


# --- taker arb A: buy Dealernet ask $258 -> sell TPX Vault bid $258 -----------
def test_arb_a_worked_example():
    dn = quote("Dealernet", bids=[(25200, 3)], asks=[(25800, 3)])
    tpx = quote("TPX:Vault", bids=[(25800, 4), (25500, 6)], asks=[(27400, 2)], is_vaulted=True)
    arb = taker_arb(dn, dn_cm(), tpx, tpx_vault_cm(), "A", sizes=(1, 3, 5))
    # cost = 258.00 * 1.00 + 15 ship in = 273.00 ; proceeds = 258 * 0.95 - 0 = 245.10
    assert arb.unit_cost_cents == Decimal(27300)
    assert arb.unit_proceeds_cents == Decimal("24510")
    assert arb.edge_per_unit_cents == Decimal("-2790")
    assert arb.edge_pct == pytest.approx(Decimal("-10.22"), abs=Decimal("0.01"))
    assert arb.max_size == 3
    f1, f3, f5 = arb.fills
    assert (f1.filled, f3.filled, f5.filled) == (1, 3, 3)
    assert f3.edge_cents == Decimal("-2790") * 3


def test_arb_b_buy_tpx_sell_dealernet_positive():
    # buy TPX Vault ask $240 (0 buyer fee, vault -> no ship in), sell Dealernet bid $265 listed (0%) minus $15 ship out
    tpx = quote("TPX:Vault", bids=[(23000, 1)], asks=[(24000, 2), (24500, 5)], is_vaulted=True)
    dn = quote("Dealernet", bids=[(26500, 2), (26000, 4)], asks=[(27000, 1)])
    arb = taker_arb(tpx, tpx_vault_cm(), dn, dn_cm(), "B", sizes=(1, 3, 5))
    assert arb.unit_cost_cents == Decimal(24000)
    assert arb.unit_proceeds_cents == Decimal(25000)  # 26500 - 1500
    assert arb.edge_per_unit_cents == Decimal(1000)
    assert arb.edge_pct == pytest.approx(Decimal("4.1667"), abs=Decimal("0.001"))
    f1, f3, f5 = arb.fills
    # size 3: buy 2 @240 + 1 @245 = 725.00 ; sell 2 @265 + 1 @260 = 790 - 45 ship = 745.00 ; edge 20.00
    assert f3.filled == 3
    assert f3.cost_cents == Decimal(72500)
    assert f3.proceeds_cents == Decimal(74500)
    assert f3.edge_cents == Decimal(2000)
    # size 5: buy 2@240 + 3@245 = 1215 ; sell 2@265 + 3@260 = 1310 - 75 = 1235 ; edge 20.00 (marginal units ~0)
    assert f5.filled == 5 and f5.edge_cents == Decimal(2000)
    assert "vault" in arb.notes[0]


def test_walk_fill_limits_to_shallower_side():
    asks = [BookLevel(price_cents=100, quantity=10)]
    bids = [BookLevel(price_cents=120, quantity=2)]
    f = walk_fill(5, asks, tpx_vault_cm(), bids, tpx_vault_cm())
    assert f.filled == 2 and f.cost_cents == Decimal(200)


# --- maker levels ------------------------------------------------------------
def test_highest_bid_on_tpx_vault_to_flip_into_dealernet_bid():
    # DN bid 265 listed -> proceeds 265 - 15 ship = 250.00. Margin 8%: max cost 231.48; TPX vault buyer fee 0,
    # vault no shipping -> bid <= 231.48 -> rounded down to $1 tick = $231.
    tpx = quote("TPX:Vault", bids=[(22500, 1)], asks=[(24000, 1)], is_vaulted=True)
    dn = quote("Dealernet", bids=[(26500, 2)], asks=[(27000, 1)])
    ml = highest_bid_to_flip(tpx_vault_cm(), tpx, dn, dn_cm(), Decimal("0.08"), "x")
    assert ml.rest_clean_cents == 23100
    assert ml.would_be_best is True and ml.improves_by_ticks == 6
    assert ml.expected_edge_pct >= Decimal(8)


def test_highest_bid_on_dealernet_to_flip_into_tpx_bid():
    # TPX Vault bid 258 -> proceeds 258*0.95 = 245.10 (vault, no ship). margin 8% -> max cost 226.94.
    # Dealernet maker: cost = p*1.03 + 15 ship in -> p <= (226.94-15)/1.03 = 205.77 -> $205.
    tpx = quote("TPX:Vault", bids=[(25800, 4)], asks=[(27400, 2)], is_vaulted=True)
    dn = quote("Dealernet", bids=[(20000, 1)], asks=[(25800, 3)])
    ml = highest_bid_to_flip(dn_cm(), dn, tpx, tpx_vault_cm(), Decimal("0.08"), "x")
    assert ml.rest_clean_cents == 20500
    assert ml.would_be_best is True and ml.improves_by_ticks == 5
    assert ml.expected_edge_pct >= Decimal(8)


def test_lowest_ask_on_tpx_ship_to_cover_from_dealernet_ask():
    # cover: DN ask 258 listed + 15 ship in = 273.00 ; need proceeds >= 273*1.08 = 294.84
    # TPX Ship maker sell: p*(1-0.01) - 15 ship out >= 294.84 -> p >= 312.97 -> round up $313
    tpx = quote("TPX:Ship", bids=[(25200, 2)], asks=[(27900, 1)])
    dn = quote("Dealernet", bids=[(25200, 3)], asks=[(25800, 3)])
    ml = lowest_ask_to_cover(tpx_ship_cm(), tpx, dn, dn_cm(), Decimal("0.08"), "x")
    assert ml.rest_clean_cents == 31300
    assert ml.would_be_best is False and ml.improves_by_ticks == -34
    assert ml.expected_edge_pct >= Decimal(8)


def test_maker_with_empty_counter_side_has_no_level():
    tpx = quote("TPX:Vault", bids=[], asks=[(27400, 2)])
    dn = quote("Dealernet", bids=[], asks=[(25800, 3)])
    ml = highest_bid_to_flip(tpx_vault_cm(), tpx, dn, dn_cm(), Decimal("0.08"), "x")
    assert ml.rest_clean_cents is None and "no bid" in ml.notes[0]


# --- fair value --------------------------------------------------------------
def test_fair_value_gaps_and_flags():
    tpx = quote("TPX:Vault", bids=[(25800, 1)], asks=[(27400, 1)], fair_value=26650, fair_value_model="v")
    dn = quote("Dealernet", bids=[(27000, 1)], asks=[(25800, 1)])
    g = fair_value_gaps(tpx, dn)
    assert g.dn_ask_gap_pct == pytest.approx(Decimal("-3.1895"), abs=Decimal("0.001"))
    assert g.dn_bid_gap_pct > 0
    assert "Dealernet ask BELOW fair value" in g.flags and "Dealernet bid ABOVE fair value" in g.flags
    assert g.tpx_mid_gap_pct == pytest.approx(Decimal("-0.1876"), abs=Decimal("0.001"))


def test_no_fair_value_says_no_fv_not_zero():
    tpx = quote("TPX:Vault", bids=[(100, 1)], asks=[(200, 1)], fair_value=None)
    g = fair_value_gaps(tpx, None)
    assert g.has_fv is False and g.flags == ["no FV"] and g.tpx_mid_gap_pct is None


# --- liquidity ---------------------------------------------------------------
def test_liquidity_flags_stale_and_thin():
    from datetime import datetime, timedelta, timezone

    now = datetime(2026, 10, 21, tzinfo=timezone.utc)
    q = quote("TPX:Ship", bids=[(25200, 2)], asks=[(27900, 1)], units_30d=6,
              top_bid_resting_since=now - timedelta(days=16), top_ask_resting_since=now - timedelta(days=1))
    li = liquidity(q, stale_days=7, thin_units=3, size_cap=10, now=now)
    assert any(f.startswith("stale bid") for f in li.flags)
    assert "thin bids" in li.flags and "thin asks" in li.flags
    assert Decimal(0) < li.factor < Decimal(1)
    # depth part = min(1,1/10)=0.1*0.4 = 0.04 ; vel = 6/10*0.6 = 0.36 ; stale halves -> 0.2
    assert li.factor == Decimal("0.200")


def test_assess_and_rank_score():
    tpx = quote("TPX:Vault", bids=[(23000, 1)], asks=[(24000, 2), (24500, 5)], is_vaulted=True, units_30d=61,
                fair_value=24500, fair_value_model="v", product_name="P")
    dn = quote("Dealernet", bids=[(26500, 2), (26000, 4)], asks=[(27000, 1)])
    a = assess(tpx, tpx_vault_cm(), dn, dn_cm(), margin=Decimal("0.08"), stale_days=7, thin_units=3, size_cap=10)
    assert a.best_arb.label.startswith("B")
    assert a.best_arb.edge_per_unit_cents == Decimal(1000)
    assert a.risk_adjusted_score > 0
    assert len(a.makers) == 4
