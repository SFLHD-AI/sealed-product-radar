"""TCGplayer page source: market price, last sale and trend without API keys."""
import json
from pathlib import Path

import httpx

from edge.tcgplayer import TCGplayerPages


def _transport(routes):
    def handler(req: httpx.Request):
        for key, body in routes.items():
            if key in str(req.url):
                return httpx.Response(200, json=body)
        return httpx.Response(404, json={})
    return httpx.MockTransport(handler)


def test_page_source_reads_market_last_sale_and_trend(tmp_path):
    routes = {
        "/pricepoints": [{"printingType": "Normal", "marketPrice": 42.5, "lowestPrice": 39.99, "listedMedianPrice": 44.0}],
        "/latestsales": {"data": [{"purchasePrice": 41.0, "orderDate": "2026-10-20T15:00:00.000+00:00"},
                                  {"purchasePrice": 43.0, "orderDate": "2026-10-21T09:30:00.000+00:00"}]},
        "/price/history/": {"result": [{"variant": "Normal", "buckets": [
            {"bucketStartDate": "2026-09-20", "marketPrice": 40.0, "quantitySold": 5},
            {"bucketStartDate": "2026-10-21", "marketPrice": 42.5, "quantitySold": 7}]}]},
    }
    src = TCGplayerPages(tmp_path, transport=_transport(routes), min_interval_s=0)
    p = src.price("123")
    assert p.market_cents == 4250 and p.low_cents == 3999 and p.mid_cents == 4400
    assert p.last_sale_cents == 4300 and p.last_sale_at.startswith("2026-10-21")
    assert [b["market"] for b in p.trend] == [4000, 4250] and p.trend_pct == 6.25
    assert p.source == "page"
    # cached on disk: a second call does not need the network
    again = TCGplayerPages(tmp_path, transport=_transport({}), min_interval_s=0).price("123")
    assert again.market_cents == 4250


def test_page_source_gives_up_when_host_is_blocked(tmp_path):
    def handler(req):
        raise httpx.ConnectError("CONNECT tunnel failed, response 403")
    src = TCGplayerPages(tmp_path, transport=httpx.MockTransport(handler), min_interval_s=0)
    assert src.price("1") is None and src.dead
    assert src.price("2") is None  # no further requests once the host is known to be blocked
