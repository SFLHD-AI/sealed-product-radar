"""`edge` CLI (typer). Read-only: there is no `place` command.

TODO(place): a future `edge place` MUST be gated behind an explicit
`--i-really-want-to-trade` confirmation flag plus an interactive confirm, and
must live in a separate module so queries.py stays mutation-free. Not built.
"""
from __future__ import annotations

import logging
import sys
import time
from decimal import Decimal
from pathlib import Path
from typing import Sequence, Optional

import typer
import yaml
from rich.console import Console
from rich.table import Table

from . import __version__
from .config import Settings, load_settings
from .dealernet.base import DealernetSource
from .dealernet.csv_source import CSVSource
from .dealernet import live_source as live_dealernet_source
from .dealernet.manual import ManualSource
from .edge import rank
from .matching import best_match, score
from .realtime import BookSync, PollingFeed
from .report import (export_csv, export_json, export_markdown, print_assessment, quote_table, summary_table, usd)
from .service import EdgeService
from .tpx.client import TPXAuthError, TPXError
from .tpx.schema_check import probe_queries, validate_schema

app = typer.Typer(help="TPX <-> Dealernet edge finder for sealed wax / sealed TCG. Read-only.", no_args_is_help=True)
console = Console(emoji=False)
err = Console(stderr=True, emoji=False)


def _settings(ctx: typer.Context) -> Settings:
    return ctx.obj["settings"]


def _service(ctx: typer.Context) -> EdgeService:
    try:
        return EdgeService(_settings(ctx), record_fixtures=ctx.obj["record"], demo_fixtures=ctx.obj["demo"])
    except RuntimeError as exc:
        err.print(f"[red]{exc}[/red]")
        raise typer.Exit(2)


def _usd_cents(v: Optional[float]) -> Optional[int]:
    return None if v is None else int(round(v * 100))


@app.callback()
def main(
    ctx: typer.Context,
    env_file: Path = typer.Option(Path(".env"), "--env", help="Path to .env"),
    vertical: Optional[str] = typer.Option(None, help="sealed_wax | sealed_tcg | both"),
    record: bool = typer.Option(False, "--record", help="Record every TPX response to tests/fixtures/"),
    verbose: bool = typer.Option(False, "-v", "--verbose"),
    demo: bool = typer.Option(False, "--demo", help="Offline: serve TPX from tests/fixtures/demo (no key needed)"),
):
    logging.basicConfig(level=logging.DEBUG if verbose else logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    settings = Settings(_env_file=env_file, **({"vertical": vertical} if vertical else {}))
    demo_dir = Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "demo"
    if demo and record:
        err.print("[red]--demo and --record are mutually exclusive[/red]")
        raise typer.Exit(2)
    ctx.obj = {"settings": settings, "record": record, "demo": demo_dir if demo else None}


@app.command()
def version():
    console.print(f"edge {__version__}")


# ---------------------------------------------------------------------------
@app.command()
def doctor(ctx: typer.Context):
    """Introspect the live TPX schema and validate every query/field this tool uses."""
    svc = _service(ctx)
    hard: list[str] = []
    try:
        schema = svc.client.introspect()
    except TPXAuthError as exc:
        err.print(f"[red]Auth failed:[/red] {exc}")
        raise typer.Exit(2)
    except TPXError:
        schema = None
    if schema is not None:
        problems = validate_schema(schema)
        hard = [p for p in problems if not p.startswith("note")]
        for p in problems:
            console.print(("[red]" if not p.startswith("note") else "[dim]") + p + "[/]")
        if not hard:
            console.print("[green]All queries and fields in edge/tpx/queries.py exist in the live schema.[/green]")
    else:
        console.print("[dim]Introspection is disabled; running every query against a live product instead.[/dim]")
        t = Table(title="Live query probe")
        t.add_column("query"); t.add_column("ok"); t.add_column("detail")
        for name, ok, detail in probe_queries(svc):
            t.add_row(name, "[green]ok[/green]" if ok else "[red]FAIL[/red]", detail[:100])
            if not ok:
                hard.append(name)
        console.print(t)
    try:
        types = svc.catalog.list_asset_types()
        if types:
            t = Table(title="Asset types (pin the sealed ones in .env)")
            t.add_column("id"); t.add_column("type"); t.add_column("name")
            for a in types:
                t.add_row(str(a.get("id")), str(a.get("type", "")), str(a.get("displayName", "")))
            console.print(t)
    except Exception as exc:  # noqa: BLE001
        console.print(f"[dim]asset type listing unavailable: {exc}[/dim]")
    raise typer.Exit(1 if hard else 0)


# ---------------------------------------------------------------------------
@app.command()
def search(ctx: typer.Context, query: str, limit: int = typer.Option(10, "--limit", "-n"),
           quick: bool = typer.Option(False, "--quick", help="Skip the MINT-book / fair-value probes")):
    """Search TPX sealed products; show assetConfigurationId, format, MINT book + FV availability."""
    svc = _service(ctx)
    try:
        products = svc.search(query, limit, enrich=not quick)
    except TPXError as exc:
        err.print(f"[red]TPX error:[/red] {exc}")
        raise typer.Exit(1)
    finally:
        svc.close()
    t = Table(title=f"TPX matches for {query!r} ({svc.settings.vertical})")
    for c in ("#", "assetConfigurationId", "product", "vertical", "format", "MINT key", "MINT book", "top bid/ask (clean)", "fair value", "match"):
        t.add_column(c)
    for i, p in enumerate(products):
        hb = p.raw.get("has_mint_book")
        fv = p.raw.get("fair_value_cents")
        t.add_row(str(i), p.asset_configuration_id, p.name, p.asset_type_name or "", (p.format or "") + (" (case)" if p.is_case else ""),
                  "yes" if p.key_id else "[red]no[/red]",
                  "—" if hb is None else ("[green]yes[/green]" if hb is True else ("[yellow]empty[/yellow]" if hb is False else str(hb))),
                  f"{usd(p.raw.get('top_bid'))} / {usd(p.raw.get('top_ask'))}" if hb is not None else "—",
                  "—" if quick else (usd(fv) if isinstance(fv, int) else ("no FV" if fv is None else str(fv))),
                  f"{score(query, p.name):.0f}")
    console.print(t)
    if not products:
        console.print("[yellow]No matches.[/yellow]")


# ---------------------------------------------------------------------------
def _dn_source(ctx: typer.Context, dn_bid, dn_ask, dn_qty, dn_bid_qty, dn_ask_qty, dn_csv, interactive: bool) -> Optional[DealernetSource]:
    s = _settings(ctx)
    if dn_csv:
        return CSVSource(dn_csv, matcher=lambda q, cands: (best_match(q, cands) or (None,))[0])
    if dn_bid is not None or dn_ask is not None:
        return ManualSource(dn_bid, dn_ask, dn_bid_qty, dn_ask_qty, dn_qty, interactive=False, console=console)
    if s.dealernet_access in ("scrape", "api"):
        return live_dealernet_source(s)
    if s.dealernet_access == "csv" and not dn_csv:
        err.print("[yellow]DEALERNET_ACCESS=csv but no --dn-csv given; falling back to manual prompt.[/yellow]")
    return ManualSource(interactive=interactive, console=console) if interactive else None


@app.command()
def compare(
    ctx: typer.Context,
    product: str,
    dn_bid: Optional[float] = typer.Option(None, "--dn-bid", help="Dealernet best bid, $"),
    dn_ask: Optional[float] = typer.Option(None, "--dn-ask", help="Dealernet best ask, $"),
    dn_qty: Optional[int] = typer.Option(None, "--dn-qty", help="Qty for both sides (default 1)"),
    dn_bid_qty: Optional[int] = typer.Option(None, "--dn-bid-qty"),
    dn_ask_qty: Optional[int] = typer.Option(None, "--dn-ask-qty"),
    dn_csv: Optional[str] = typer.Option(None, "--dn-csv", help="CSV file ('-' = stdin paste), see README"),
    dn_name: Optional[str] = typer.Option(None, "--dn-name", help="Exact Dealernet product name in the CSV"),
    margin: Optional[float] = typer.Option(None, "--margin", help="Target margin on capital, e.g. 0.08"),
    shipping: Optional[float] = typer.Option(None, "--shipping", help="Shipping $ per physical move (overrides box/case)"),
    pick: Optional[int] = typer.Option(None, "--pick", help="Index from `edge search` when ambiguous"),
    config_id: Optional[str] = typer.Option(None, "--id", help="Exact TPX assetConfigurationId"),
    json_out: Optional[Path] = typer.Option(None, "--json"),
    csv_out: Optional[Path] = typer.Option(None, "--csv"),
    md_out: Optional[Path] = typer.Option(None, "--md"),
    no_prompt: bool = typer.Option(False, "--no-prompt", help="TPX-only view if no Dealernet data given"),
):
    """Full side-by-side vs Dealernet, maker levels, FV gaps, liquidity, ranked by TPX market."""
    svc = _service(ctx)
    m = Decimal(str(margin)) if margin is not None else None
    try:
        prod = svc.resolve_product(product, pick=pick, exact_id=config_id)
        console.print(f"[bold]{prod.name}[/bold]  id={prod.asset_configuration_id}  key=MINT({prod.key_id})  "
                      f"format={prod.format or '?'}{' CASE' if prod.is_case else ''}")
        src = _dn_source(ctx, dn_bid, dn_ask, dn_qty, dn_bid_qty, dn_ask_qty, dn_csv, interactive=not no_prompt)
        cands = src.products() if isinstance(src, CSVSource) else None
        pq, assessments = svc.compare(prod, src, margin=m, shipping_cents=_usd_cents(shipping), dn_name=dn_name, dn_candidates=cands)
    except (TPXError, LookupError, NotImplementedError, ValueError) as exc:
        err.print(f"[red]{exc}[/red]")
        raise typer.Exit(1)
    finally:
        svc.close()

    if pq.dn is None:
        console.print("[yellow]No Dealernet quote: showing TPX-only metrics.[/yellow]")
    else:
        console.print(f"Dealernet product matched via: {pq.dn_match}")
    console.print(quote_table([q for q, _ in pq.tpx], pq.dn))
    for cm in [cm for _, cm in pq.tpx] + ([pq.dn_cm] if pq.dn_cm else []):
        console.print(f"[dim]{cm.venue}: {cm.kind.value} buyer {cm.buyer_fee_bps}bps seller {cm.seller_fee_bps}bps "
                      f"maker {cm.maker_fee_bps}bps taker {cm.taker_fee_bps}bps; ship in/out {usd(cm.shipping_in_cents)}/{usd(cm.shipping_out_cents)}; "
                      f"tick {usd(cm.tick_size_cents)} ({cm.source})[/dim]")
    ranked = rank(assessments)
    mm = m if m is not None else svc.settings.target_margin
    for a in ranked:
        console.rule(a.tpx_market)
        print_assessment(console, a, mm)
    console.print(summary_table(ranked))
    _exports(ranked, json_out, csv_out, md_out, meta={"product": prod.name, "margin": str(mm)})


def _exports(ranked, json_out, csv_out, md_out, meta=None):
    if json_out:
        export_json(ranked, json_out, meta)
        console.print(f"wrote {json_out}")
    if csv_out:
        export_csv(ranked, csv_out)
        console.print(f"wrote {csv_out}")
    if md_out:
        export_markdown(ranked, md_out)
        console.print(f"wrote {md_out}")


# ---------------------------------------------------------------------------
@app.command()
def watchlist(
    ctx: typer.Context,
    path: Path = typer.Argument(..., help="watchlist.yaml (see README)"),
    dn_csv: Optional[str] = typer.Option(None, "--dn-csv", help="Dealernet CSV covering the watchlist"),
    margin: Optional[float] = typer.Option(None, "--margin"),
    csv_out: Optional[Path] = typer.Option(Path("watchlist_edges.csv"), "--csv"),
    json_out: Optional[Path] = typer.Option(None, "--json"),
):
    """Batch compare every product in a YAML watchlist; ranked table + CSV export."""
    m = Decimal(str(margin)) if margin is not None else None
    ranked = rank(_run_watchlist(ctx, path, dn_csv, m, with_candles=False))
    console.print(summary_table(ranked))
    _exports(ranked, json_out, csv_out, None, meta={"watchlist": str(path)})


def _watchlist_items(path: Path) -> list[dict]:
    data = yaml.safe_load(Path(path).read_text()) or {}
    items = data if isinstance(data, list) else data.get("products") or []
    return [{"name": i} if isinstance(i, str) else i for i in items]


def _run_watchlist(ctx: typer.Context, path: Path, dn_csv: Optional[str], m: Optional[Decimal], *,
                   with_candles: bool, errors: Optional[list[str]] = None, extra_items: Sequence[dict] = ()):
    """Compare every watchlist product (plus `extra_items`, same shape, de-duplicated by
    tpx_id); returns the unranked assessments. Per-product failures are printed (and
    collected into `errors`) and skipped."""
    svc = _service(ctx)
    csv_src = CSVSource(dn_csv, matcher=lambda q, c: (best_match(q, c) or (None,))[0]) if dn_csv else None
    all_assessments = []

    def fail(msg: str) -> None:
        err.print(f"[red]{msg}[/red]")
        if errors is not None:
            errors.append(msg)

    live_src: Optional[DealernetSource] = None
    if csv_src is None:
        try:
            live_src = live_dealernet_source(svc.settings)
        except (RuntimeError, NotImplementedError) as exc:
            fail(f"Dealernet ({svc.settings.dealernet_access}): {exc}")

    items = list(_watchlist_items(path))
    seen_ids = {i.get("tpx_id") for i in items if i.get("tpx_id")}
    seen_names = {i["name"].lower() for i in items}
    for extra in extra_items:
        if extra.get("tpx_id") in seen_ids or extra["name"].lower() in seen_names:
            continue
        items.append(extra)
        seen_ids.add(extra.get("tpx_id"))
    try:
        for item in items:
            name = item["name"]
            try:
                prod = svc.resolve_product(name, exact_id=item.get("tpx_id"))
            except (TPXError, LookupError) as exc:
                fail(f"{name}: {exc}")
                continue
            src: Optional[DealernetSource] = csv_src or live_src
            if item.get("dn_bid") is not None or item.get("dn_ask") is not None:
                src = ManualSource(item.get("dn_bid"), item.get("dn_ask"), item.get("dn_bid_qty"), item.get("dn_ask_qty"),
                                   item.get("dn_qty"), interactive=False)
            ship = item.get("shipping")
            try:
                _, assessments = svc.compare(prod, src, margin=m, shipping_cents=_usd_cents(ship) if ship is not None else None,
                                             dn_name=item.get("dn_name"), dn_candidates=csv_src.products() if csv_src else None,
                                             with_candles=with_candles,
                                             tcgplayer_id=str(item["tcgplayer_id"]) if item.get("tcgplayer_id") else None)
            except (TPXError, NotImplementedError) as exc:
                fail(f"{name}: {exc}")
                continue
            all_assessments.extend(assessments)
            console.print(f"[dim]{prod.name}: {len(assessments)} TPX market(s)[/dim]")
    finally:
        svc.close()
    return all_assessments


# ---------------------------------------------------------------------------
@app.command()
def dashboard(
    ctx: typer.Context,
    path: Path = typer.Argument(..., help="watchlist.yaml (see README)"),
    dn_csv: Optional[str] = typer.Option(None, "--dn-csv", help="Dealernet CSV covering the watchlist"),
    margin: Optional[float] = typer.Option(None, "--margin"),
    out: Path = typer.Option(Path("dashboard.html"), "--out", "-o", help="HTML file to write"),
    history_path: Path = typer.Option(Path(".cache/history.jsonl"), "--history", help="Snapshot log used for edge trends"),
    refresh: Optional[int] = typer.Option(None, "--refresh", help="Re-run every N seconds and rewrite the page (it reloads itself)"),
    open_browser: bool = typer.Option(False, "--open", help="Open the page in your browser"),
    universe: int = typer.Option(0, "--universe", help="Also price the N hottest TPX products per vertical (30-day volume) "
                                                       "and embed the full product catalogue for the page's search"),
    iterations: Optional[int] = typer.Option(None, "--iterations", hidden=True),
):
    """Write a view-only HTML board: your products as tiles, TPX vs Dealernet, trends and outlook."""
    from . import dashboard as dash
    from . import history as hist

    s = _settings(ctx)
    source = "demo" if ctx.obj["demo"] is not None else "live"
    m = Decimal(str(margin)) if margin is not None else s.target_margin
    uni_rows: list[dict] = []
    hot: list[dict] = []
    if source == "live":
        from .universe import Universe, hottest
        svc = _service(ctx)
        uni = Universe(svc.client, svc.catalog, s.cache_dir)
        if universe > 0:
            uni_rows = uni.load()
        else:  # quick refresh: embed the cached catalogue for search, price only the watchlist
            uni_rows = uni.load_cached()
        for vertical in ("sealed_wax", "sealed_tcg") if universe > 0 else ():
            hot += [{"name": r["name"], "tpx_id": r["id"],
                     "shipping": 4 if any(w in r["name"].lower() for w in ("pack", "tin", "blister")) else None}
                    for r in hottest(uni_rows, universe, vertical)]
    i = 0
    try:
        while True:
            i += 1
            errors: list[str] = []
            ranked = rank(_run_watchlist(ctx, path, dn_csv, m, with_candles=True, errors=errors, extra_items=hot))
            hist.append(history_path, ranked, source)
            page = dash.render(ranked, source=source, actions_enabled=s.actions_enabled, margin=m,
                               history=hist.load(history_path, source), refresh_s=refresh, warnings=errors,
                               universe=uni_rows)
            out.write_text(page, encoding="utf-8")
            console.print(f"wrote {out} ({len(ranked)} product/market rows, {source} data)")
            if open_browser and i == 1:
                import webbrowser
                webbrowser.open(out.resolve().as_uri())
            if not refresh or (iterations is not None and i >= iterations):
                break
            time.sleep(refresh)
    except KeyboardInterrupt:
        pass


# ---------------------------------------------------------------------------
map_app = typer.Typer(help="Review / edit TPX <-> Dealernet name mappings (mappings.yaml).")
app.add_typer(map_app, name="map")


@map_app.callback(invoke_without_command=True)
def map_list(ctx: typer.Context):
    if ctx.invoked_subcommand:
        return
    s = _settings(ctx)
    from .matching import Mappings
    mp = Mappings(s.mappings_path)
    t = Table(title=f"mappings ({s.mappings_path})")
    for c in ("tpx_id", "tpx_name", "dealernet_name", "dealernet_url"):
        t.add_column(c)
    for p in mp.products:
        t.add_row(str(p.get("tpx_id")), str(p.get("tpx_name")), str(p.get("dealernet_name")), str(p.get("dealernet_url", "")))
    console.print(t)


@map_app.command("add")
def map_add(ctx: typer.Context, tpx_id: str, dealernet_name: str,
            tpx_name: str = typer.Option("", "--tpx-name"), url: Optional[str] = typer.Option(None, "--url")):
    from .matching import Mappings
    mp = Mappings(_settings(ctx).mappings_path)
    mp.set(tpx_id, tpx_name, dealernet_name, url)
    console.print(f"mapped {tpx_id} -> {dealernet_name!r}")


@map_app.command("remove")
def map_remove(ctx: typer.Context, tpx_id: str):
    from .matching import Mappings
    mp = Mappings(_settings(ctx).mappings_path)
    console.print("removed" if mp.remove(tpx_id) else "not found")


@map_app.command("suggest")
def map_suggest(ctx: typer.Context, dn_csv: str, threshold: float = 70.0):
    """Fuzzy-suggest TPX matches for every Dealernet product in a CSV (searches TPX per product)."""
    svc = _service(ctx)
    src = CSVSource(dn_csv)
    t = Table(title="Suggested mappings (add with `edge map add <tpx_id> \"<dealernet name>\"`)")
    for c in ("dealernet_name", "tpx_id", "tpx_name", "score"):
        t.add_column(c)
    try:
        for dn_name in src.products():
            try:
                prods = svc.search(dn_name, 5)
            except TPXError as exc:
                t.add_row(dn_name, f"[red]{exc}[/red]", "", "")
                continue
            bm = best_match(dn_name, [p.name for p in prods], threshold)
            if bm:
                p = next(p for p in prods if p.name == bm[0])
                t.add_row(dn_name, p.asset_configuration_id, p.name, f"{bm[1]:.0f}")
            else:
                t.add_row(dn_name, "[yellow]no match[/yellow]", "", "")
    finally:
        svc.close()
    console.print(t)


# ---------------------------------------------------------------------------
@app.command()
def watch(
    ctx: typer.Context,
    product: str,
    dn_bid: Optional[float] = typer.Option(None, "--dn-bid"),
    dn_ask: Optional[float] = typer.Option(None, "--dn-ask"),
    dn_qty: Optional[int] = typer.Option(None, "--dn-qty"),
    dn_csv: Optional[str] = typer.Option(None, "--dn-csv"),
    interval: float = typer.Option(10.0, "--interval", help="TPX poll seconds (socket transport pending docs)"),
    dn_refresh: float = typer.Option(300.0, "--dn-refresh", help="Re-read Dealernet CSV every N seconds"),
    alert_usd: float = typer.Option(10.0, "--alert", help="Bell when best edge/unit crosses this $"),
    margin: Optional[float] = typer.Option(None, "--margin"),
    shipping: Optional[float] = typer.Option(None, "--shipping", help="Shipping $ per physical move"),
    iterations: Optional[int] = typer.Option(None, "--iterations", hidden=True),
):
    """Live-ish TPX book (poll now; socket when TPX_SOCKET_URL is configured) + periodic Dealernet refresh."""
    svc = _service(ctx)
    m = Decimal(str(margin)) if margin is not None else None
    try:
        prod = svc.resolve_product(product)
    except (TPXError, LookupError) as exc:
        err.print(f"[red]{exc}[/red]")
        raise typer.Exit(1)
    if svc.settings.tpx_socket_url:
        err.print("[yellow]TPX_SOCKET_URL set but the socket transport is not implemented yet (needs the docs' message framing); polling.[/yellow]")
    syncs = {}
    last_dn = 0.0
    src = None
    i = 0
    try:
        while iterations is None or i < iterations:
            i += 1
            now = time.time()
            if src is None or (dn_csv and now - last_dn >= dn_refresh):
                src = _dn_source(ctx, dn_bid, dn_ask, dn_qty, None, None, dn_csv, interactive=False)
                last_dn = now
            svc.client.cache.clear()
            cands = src.products() if isinstance(src, CSVSource) else None
            pq, assessments = svc.compare(prod, src, margin=m, shipping_cents=_usd_cents(shipping),
                                          dn_candidates=cands, with_candles=False)
            for q, _ in pq.tpx:
                sync = syncs.setdefault(q.market_id, BookSync(prod.asset_configuration_id, prod.key_id))
                sync.snapshot({"sequence": q.raw.get("book_sequence"), "bids": [l.model_dump() for l in q.bids], "asks": [l.model_dump() for l in q.asks]})
            ranked = rank(assessments)
            console.clear()
            console.print(f"[bold]{prod.name}[/bold]  {time.strftime('%H:%M:%S')}  poll #{i}  channels: {', '.join(next(iter(syncs.values())).channels) if syncs else ''}")
            console.print(summary_table(ranked))
            best = ranked[0].best_arb if ranked else None
            if best and best.edge_per_unit_cents is not None and best.edge_per_unit_cents >= Decimal(alert_usd) * 100:
                console.print(f"[bold green]EDGE {usd(best.edge_per_unit_cents)}/unit on {best.label}[/bold green]\a")
            if iterations is None or i < iterations:
                time.sleep(interval)
    except KeyboardInterrupt:
        pass
    finally:
        svc.close()


if __name__ == "__main__":  # pragma: no cover
    app()
