"""Hosted app: board storage, auth, and a demo refresh served over HTTP."""
from __future__ import annotations

import base64
import json
import threading
import time
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from edge.webapp import Radar, clean_board, make_handler, watchlist_items


def test_clean_board_validates_and_clamps():
    b = clean_board({"products": [{"name": " X ", "tpx_id": "a"}, {"name": ""}, "junk"], "hours": 0.01})
    assert b == {"products": [{"name": "X", "tpx_id": "a"}], "hours": 0.25}
    assert clean_board({"hours": 0})["hours"] == 0
    assert clean_board({"hours": "x"})["hours"] == 1


def test_watchlist_items_ship_packs_cheaply():
    items = watchlist_items({"products": [{"name": "Booster Pack"}, {"name": "Hobby Box", "tpx_id": "t"}]})
    assert items == [{"name": "Booster Pack", "shipping": 4}, {"name": "Hobby Box", "tpx_id": "t"}]


def test_schedule(tmp_path):
    r = Radar(tmp_path)
    assert r.due() == "full"  # no page yet
    r.page_path.write_text("x")
    r.save_board({"products": [], "hours": 1})
    r.last_run = r.last_full = time.time()
    assert r.due() is None
    r.last_run = r.last_full = time.time() - 3700
    assert r.due() == "full"
    r.last_full = time.time() - 60
    assert r.due() == "board"
    r.save_board({"products": [], "hours": 0})
    assert r.due() is None


@pytest.fixture
def server(tmp_path):
    radar = Radar(tmp_path, demo=True)
    srv = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(radar, "pw"))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield radar, f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


def _req(url, method="GET", body=None, pw="pw"):
    req = urllib.request.Request(url, method=method, data=json.dumps(body).encode() if body is not None else None)
    if pw:
        req.add_header("Authorization", "Basic " + base64.b64encode(f"ross:{pw}".encode()).decode())
    with urllib.request.urlopen(req, timeout=10) as res:
        return res.status, res.read().decode()


def test_http_auth_board_and_refresh(server):
    radar, base = server
    assert _req(base + "/healthz", pw=None)[0] == 200
    with pytest.raises(urllib.error.HTTPError) as e:
        _req(base + "/api/board", pw=None)
    assert e.value.code == 401
    with pytest.raises(urllib.error.HTTPError):
        _req(base + "/api/board", pw="wrong")
    assert json.loads(_req(base + "/api/board")[1])["products"]  # seeded from board.seed.json
    _req(base + "/api/board", "PUT", {"products": [], "hours": 2})
    assert json.loads(_req(base + "/api/board")[1]) == {"products": [], "hours": 2}
    assert "first price pull" in _req(base + "/")[1]

    assert json.loads(_req(base + "/api/refresh", "POST", {})[1])["started"] is True
    for _ in range(240):
        if not radar.running:
            break
        time.sleep(0.5)
    st = json.loads(_req(base + "/api/status")[1])
    assert st["last_error"] is None and st["page_at"]
    page = _req(base + "/")[1]
    assert "window.RADAR_API=true" in page and "Live Sealed Product Radar" in page
