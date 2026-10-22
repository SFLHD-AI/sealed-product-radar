"""End-to-end against the demo fixtures (mock TPX)."""
import json
from decimal import Decimal
from pathlib import Path

from typer.testing import CliRunner

from edge.cli import app
from edge.dealernet.manual import ManualSource
from edge.edge import rank
from edge.tpx.schema_check import validate_schema

import pytest

import edge.cli as cli_mod

runner = CliRunner()
DEMO = Path(__file__).parent / "fixtures" / "demo"


@pytest.fixture(autouse=True)
def wide_console():
    cli_mod.console.width = 250
    yield


def test_search_resolves_asset_types_and_mint_key(service):
    prods = service.search("2024 topps chrome baseball hobby", enrich=True)
    assert prods[0].name == "2024 Topps Chrome Baseball Hobby Box"
    assert prods[0].key_id == "key_wax_mint"
    assert prods[0].raw["has_mint_book"] is True and prods[0].raw["fair_value_cents"] == 26650
    case = next(p for p in prods if p.is_case)
    assert case.raw["has_mint_book"] is False and case.raw["fair_value_cents"] is None
    # condition key cached per asset type
    assert service.catalog.ids.get("key:at_sealed_wax:MINT") == "key_wax_mint"


def test_compare_builds_both_markets_with_fees_from_getmarket(service):
    prod = service.resolve_product("2024 topps chrome baseball hobby")
    pq, assessments = service.compare(prod, ManualSource(bid=252, ask=258, qty=3, interactive=False))
    venues = {q.venue: cm for q, cm in pq.tpx}
    assert set(venues) == {"TPX:Vault", "TPX:Ship"}
    assert venues["TPX:Vault"].seller_fee_bps == 500 and venues["TPX:Vault"].shipping_in_cents == 0
    assert venues["TPX:Ship"].taker_fee_bps == 400 and venues["TPX:Ship"].shipping_in_cents == 1500
    vault = next(q for q, _ in pq.tpx if q.venue == "TPX:Vault")
    assert vault.best_bid == 25800 and vault.fair_value == 26650 and vault.units_30d == 61
    assert vault.candles_30d.bars == 30 and vault.candles_7d.bars == 7
    assert pq.dn.best_ask == 25800 and pq.dn_cm.maker_fee_bps == 300
    ranked = rank(assessments)
    # Vault: A = buy DN 258 +15 ship = 273 vs sell 258*0.95 = 245.10 -> negative; B = buy 274 vs sell DN 252-15 -> negative
    a = next(x for x in ranked if x.tpx_market == "TPX:Vault")
    assert a.arb_a.edge_per_unit_cents == Decimal("-2790")
    assert a.fv.has_fv and "Dealernet ask BELOW fair value" in a.fv.flags
    assert a.makers[0].rest_clean_cents is not None


def test_cli_search_demo():
    r = runner.invoke(app, ["--demo", "search", "2024 topps chrome baseball hobby", "--quick"])
    assert r.exit_code == 0, r.output
    assert "cfg_24tc_hobby" in r.output


def test_cli_compare_demo_json(tmp_path):
    out = tmp_path / "o.json"
    r = runner.invoke(app, ["--demo", "compare", "pokemon 151 booster bundle", "--dn-bid", "39", "--dn-ask", "40",
                            "--dn-qty", "10", "--shipping", "6", "--json", str(out)])
    assert r.exit_code == 0, r.output
    doc = json.loads(out.read_text())
    assert doc["meta"]["product"].startswith("Pokemon")
    vault = next(a for a in doc["assessments"] if a["tpx_market"] == "TPX:Vault")
    # A: buy DN 40.00 + 6 ship = 46.00 ; sell vault bid 39.00*0.95 = 37.05 -> -8.95
    assert Decimal(vault["arb_a_buy_dn_sell_tpx"]["edge_per_unit_cents_all_in"]) == Decimal("-895")
    assert vault["fair_value"]["fair_value_cents"] == 4080


def test_cli_watchlist_demo(tmp_path):
    wl = tmp_path / "wl.yaml"
    wl.write_text("products:\n  - name: 2024 topps chrome baseball hobby\n    dn_bid: 252\n    dn_ask: 258\n"
                  "  - name: pokemon 151 booster bundle\n    dn_bid: 39\n    dn_ask: 40\n    shipping: 6\n")
    csv_out = tmp_path / "edges.csv"
    r = runner.invoke(app, ["--demo", "watchlist", str(wl), "--csv", str(csv_out)])
    assert r.exit_code == 0, r.output
    lines = csv_out.read_text().splitlines()
    assert lines[0].startswith("rank,product,tpx_market")
    assert len(lines) == 5  # header + 2 products x 2 markets


def test_cli_compare_demo_csv_source(tmp_path):
    csv = tmp_path / "dn.csv"
    csv.write_text("product,side,price,qty\n24 Topps Chrome BB Hobby,bid,250,2\n24 Topps Chrome BB Hobby,ask,262,1\n")
    r = runner.invoke(app, ["--demo", "compare", "2024 topps chrome baseball hobby", "--dn-csv", str(csv)])
    assert r.exit_code == 0, r.output
    assert "fuzzy" in r.output


def test_cli_without_key_explains(tmp_path, monkeypatch):
    monkeypatch.delenv("TPX_API_KEY", raising=False)
    r = runner.invoke(app, ["--env", str(tmp_path / "nope.env"), "search", "x"])
    assert r.exit_code == 2 and "TPX_API_KEY" in r.output


def test_schema_validator_reports_missing():
    schema = {"queryType": {"name": "Query"}, "mutationType": None, "types": [
        {"kind": "OBJECT", "name": "Query", "fields": [
            {"name": "getOrderBook", "args": [{"name": "input", "type": {"kind": "NON_NULL", "ofType": {"kind": "INPUT_OBJECT", "name": "GetOrderBookInput"}}}],
             "type": {"kind": "OBJECT", "name": "GetOrderBookPayload"}}]},
        {"kind": "OBJECT", "name": "GetOrderBookPayload", "fields": [{"name": "bids"}, {"name": "asks"}, {"name": "error"}]},
    ]}
    probs = validate_schema(schema)
    assert any("GetOrderBookPayload.summary not found" in p for p in probs)
    assert any("MISSING: Query.getTicker" in p for p in probs)
    assert not any("MISSING: Query.listAssetTypes" in p for p in probs)  # optional -> note


def test_cli_watch_single_iteration_demo():
    r = runner.invoke(app, ["--demo", "watch", "pokemon 151 booster bundle", "--dn-bid", "46", "--dn-ask", "47",
                            "--dn-qty", "10", "--shipping", "2", "--iterations", "1", "--alert", "1"])
    assert r.exit_code == 0, r.output
    assert "public:book:cfg_sv151_bundle:key_tcg_mint" in r.output
    assert "EDGE" in r.output  # alert fired: vault B edge is positive


def test_rank_puts_rows_without_dealernet_last(service):
    prod = service.resolve_product("2024 topps chrome baseball hobby")
    _, with_dn = service.compare(prod, ManualSource(bid=252, ask=258, qty=3, interactive=False), with_candles=False)
    _, without = service.compare(prod, None, with_candles=False)
    ranked = rank(with_dn + without)
    assert all(a.best_arb is not None for a in ranked[:2]) and all(a.best_arb is None for a in ranked[2:])
