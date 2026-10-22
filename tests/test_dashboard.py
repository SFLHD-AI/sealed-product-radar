"""Dashboard + snapshot history, run end to end against the demo fixtures."""
from __future__ import annotations

import json
import re

from typer.testing import CliRunner

from edge.cli import app
from edge.dashboard import price_chart, sparkline

runner = CliRunner()


def _run(tmp_path, *extra, env=None):
    out = tmp_path / "dash.html"
    hist = tmp_path / "history.jsonl"
    res = runner.invoke(app, ["--demo", "dashboard", "watchlist.example.yaml", "--dn-csv", "dealernet.example.csv",
                              "-o", str(out), "--history", str(hist), *extra], env=env)
    assert res.exit_code == 0, res.output
    return out.read_text(), hist


def test_dashboard_renders_demo(tmp_path):
    page, hist = _run(tmp_path)
    assert "<title>Live Sealed Product Radar</title>" in page
    assert "Sample data" in page  # demo is always labelled
    assert "Actions: OFF" in page
    assert "2024 Topps Chrome Baseball Hobby Box" in page
    assert '"closes":[' in page  # price history embedded for the page's charts
    rows = [json.loads(l) for l in hist.read_text().splitlines()]
    assert rows and all(r["source"] == "demo" for r in rows)


def test_dashboard_has_no_action_controls(tmp_path):
    page, _ = _run(tmp_path)
    # Page controls (help, product list) are fine; nothing may submit anywhere or call a marketplace.
    assert not re.search(r"<form\b", page, re.I)
    assert "XMLHttpRequest" not in page and "tradepost.co/graphql" not in page
    # The only network calls are the hosted app's own same-origin /api endpoints (board + refresh).
    assert page.count("fetch(") == 1
    paths = re.findall(r"api\('(?:GET|PUT|POST)','([^']+)'", page)
    assert paths and all(p.startswith("/api/") for p in paths)


def test_actions_toggle_is_shown(tmp_path):
    page, _ = _run(tmp_path, env={"ACTIONS_ENABLED": "true"})
    assert "Actions: ON" in page


def test_history_accumulates_into_edge_trend(tmp_path):
    _run(tmp_path)
    page, hist = _run(tmp_path)
    assert len(hist.read_text().splitlines()) == 12  # 6 product/market rows x 2 runs
    assert '"outlook":{' in page  # each product carries its trend analysis


def test_chart_helpers_handle_empty_and_flat():
    assert "No price history" in price_chart({}, {"DN bid": None})
    assert price_chart({"TPX:Vault": [{"t": "2026-10-01", "close": 100}]}, {"DN bid": 100}).startswith("<svg")
    assert sparkline([5.0]) == ""
    assert sparkline([5.0, 5.0]).startswith("<svg")
