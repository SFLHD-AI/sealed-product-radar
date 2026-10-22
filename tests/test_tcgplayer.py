"""TCGplayer reference price: token caching, price parsing, % of market, dashboard column."""
from __future__ import annotations

from decimal import Decimal

import httpx

from edge.dashboard import render
from edge.tcgplayer import TCGplayerClient, pct_of


def _transport(calls: list[str]):
    def handler(req: httpx.Request) -> httpx.Response:
        calls.append(f"{req.method} {req.url.path}")
        if req.url.path == "/token":
            assert b"client_id=pub" in req.content and b"client_secret=priv" in req.content
            return httpx.Response(200, json={"access_token": "tok", "expires_in": 1209599})
        assert req.headers["Authorization"] == "bearer tok"
        return httpx.Response(200, json={"success": True, "results": [
            {"productId": 684450, "subTypeName": "Normal", "marketPrice": 70.12, "lowPrice": 66.0,
             "midPrice": 72.5, "directLowPrice": None},
        ]})
    return httpx.MockTransport(handler)


def test_prices_and_token_cache(tmp_path):
    calls: list[str] = []
    c = TCGplayerClient("pub", "priv", "https://api.tcgplayer.com", tmp_path, transport=_transport(calls))
    p = c.prices(["684450"])["684450"]
    assert (p.market_cents, p.low_cents, p.mid_cents, p.direct_low_cents) == (7012, 6600, 7250, None)
    c.prices(["684450"])
    c2 = TCGplayerClient("pub", "priv", "https://api.tcgplayer.com", tmp_path, transport=_transport(calls))
    c2.prices(["684450"])
    assert calls.count("POST /token") == 1  # token reused from disk


def test_pct_of():
    assert pct_of(6444, 7012) == Decimal("91.9")
    assert pct_of(None, 7012) is None and pct_of(6444, None) is None and pct_of(6444, 0) is None


def test_dashboard_shows_pct_of_tcgplayer(service):
    prod = service.resolve_product("2024 Topps Chrome Baseball Hobby Box")
    _, assessments = service.compare(prod, None, with_candles=False)
    for a in assessments:
        a.tpx.tcg_market = 20000
        a.tpx.raw["product_meta"] = {"asset_type": "sealed_tcg"}  # TCG tiles compare against TCGplayer
    page = render(assessments, source="demo")
    assert '"tcg":{"market":20000' in page  # the page shows bids/asks as % of this
