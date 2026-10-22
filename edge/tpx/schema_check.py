"""Validate queries.py against a live (or recorded) introspection result."""
from __future__ import annotations

from typing import Iterable

from . import queries as Q


def _unwrap(t: dict) -> str | None:
    while t and t.get("kind") in ("NON_NULL", "LIST"):
        t = t.get("ofType") or {}
    return t.get("name") if t else None


def validate_schema(schema: dict) -> list[str]:
    """Return a list of human-readable problems (empty == all good)."""
    problems: list[str] = []
    types = {t["name"]: t for t in schema.get("types", []) if t.get("name")}
    query_type = types.get((schema.get("queryType") or {}).get("name") or "Query")
    if not query_type:
        return ["schema has no Query type?!"]
    qfields = {f["name"]: f for f in query_type.get("fields") or []}

    for name, spec in Q.EXPECTED_QUERIES.items():
        f = qfields.get(name)
        if not f:
            level = "note" if spec.get("optional") else "MISSING"
            problems.append(f"{level}: Query.{name} not in live schema")
            continue
        if spec.get("input"):
            arg_types = {a["name"]: _unwrap(a["type"]) for a in f.get("args") or []}
            if arg_types.get("input") != spec["input"]:
                problems.append(f"Query.{name}(input:) is {arg_types.get('input')!r}, expected {spec['input']!r}")
        payload = types.get(_unwrap(f["type"]) or "")
        if payload:
            have = {pf["name"] for pf in payload.get("fields") or []}
            for want in spec["fields"]:
                if want not in have:
                    problems.append(f"{payload['name']}.{want} not found (used by {name})")
            if "error" not in have:
                problems.append(f"note: {payload['name']} has no `error` field")

    mutation_type = (schema.get("mutationType") or {}).get("name")
    if mutation_type:
        problems.append(f"note: schema exposes mutations via {mutation_type}; this tool never calls them")
    return problems


def key_configuration_enum_values(schema: dict, enum_name: str = "KeyType") -> Iterable[str]:
    for t in schema.get("types", []):
        if t.get("name") == enum_name and t.get("enumValues"):
            return [v["name"] for v in t["enumValues"]]
    return []


def probe_queries(svc, sample: str = "topps chrome") -> list[tuple[str, bool, str]]:
    """Run every read query once against a real product.

    The live API has introspection disabled and answers any unknown field or
    argument with an opaque INVALID_REQUEST, so this is how `edge doctor`
    checks queries.py. Returns [(query, ok, detail)].
    """
    from .client import TPXError

    out: list[tuple[str, bool, str]] = []

    def step(name: str, fn):
        try:
            detail = fn()
            out.append((name, True, str(detail or "")))
            return True
        except (TPXError, RuntimeError, LookupError, KeyError) as exc:
            out.append((name, False, str(exc)))
            return False

    products: list = []
    step("assetTypes", lambda: f"{len(svc.catalog.list_asset_types())} types")
    if not step("getFilterBuilderData", lambda: products.extend(svc.catalog.search(sample, svc.settings.vertical, 3))
                or f"{len(products)} products for {sample!r}"):
        return out
    if not products:
        return out
    p = products[0]
    if not step("listKeyConfigurations", lambda: setattr(p, "key_id", svc.catalog.resolve_condition_key(p.asset_type_id, "MINT"))
                or f"MINT key {p.key_id}"):
        return out
    markets: list = []
    step("getProductMarkets", lambda: markets.extend(svc.market.product_markets(p)) or f"{len(markets)} markets for {p.name}")
    mid = str(markets[0]["id"]) if markets else None
    if mid:
        step("getMarket", lambda: svc.market.market_cost_model(mid, mid, 0, 0, False).kind)
    step("getOrderBook", lambda: (svc.market.order_book(p, mid, 5).get("summary") or {}).get("topBidCents"))
    step("getTicker", lambda: svc.market.ticker(p, mid).get("lastPriceCents"))
    step("getRecentTrades", lambda: f"{len(svc.market.recent_trades(p, mid, 5))} trades")
    step("getCandles", lambda: f"{len(svc.market.candles(p, mid, 'D1', 30))} bars")
    step("getProductMarket", lambda: (svc.market.product_market_stats(p, mid).get("stats") or {}).get("unitVolume"))
    step("getKeyFairValuesByAssetConfig", lambda: svc.market.fair_value(p)[0] or "no FV")
    if mid:
        step("getOrderQuote", lambda: svc.market.order_quote(p, mid, "BUY", 0, 1).get("fillableQuantity"))
    return out
