"""Rich tables + CSV / JSON / markdown export. Every figure is labelled clean
or all-in and carries its source + timestamp."""
from __future__ import annotations

import csv
import io
import json
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterable, Optional, Sequence

from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from .edge import Assessment, MakerLevel, TakerArb
from .models import VenueQuote


# --- formatting ------------------------------------------------------------
def usd(cents: Optional[Decimal | int], signed: bool = False) -> str:
    if cents is None:
        return "—"
    d = Decimal(cents) / 100
    s = f"{d:,.2f}"
    if signed and d > 0:
        s = "+" + s
    return f"${s}" if not s.startswith(("-", "+")) else s[0] + "$" + s[1:]


def pct(v: Optional[Decimal], signed: bool = True) -> str:
    if v is None:
        return "—"
    return f"{v:+.1f}%" if signed else f"{v:.1f}%"


def n(v: Optional[int]) -> str:
    return "—" if v is None else f"{v:,}"


def ts(d: Optional[datetime]) -> str:
    return "—" if d is None else d.strftime("%Y-%m-%d %H:%MZ")


def colour_edge(cents: Optional[Decimal]) -> str:
    if cents is None:
        return "—"
    s = usd(cents, signed=True)
    return f"[green]{s}[/green]" if cents > 0 else f"[red]{s}[/red]"


# --- tables ----------------------------------------------------------------
def quote_table(tpx_quotes: Sequence[VenueQuote], dn: Optional[VenueQuote]) -> Table:
    t = Table(title="Side-by-side (CLEAN prices, pre-fee)", show_lines=False)
    t.add_column("metric", style="bold")
    cols = list(tpx_quotes) + ([dn] if dn else [])
    for q in cols:
        t.add_column(q.venue + (" (vault)" if q.is_vaulted else ""), justify="right")

    def row(label, fn):
        t.add_row(label, *[fn(q) for q in cols])

    row("best bid", lambda q: usd(q.best_bid))
    row("best ask", lambda q: usd(q.best_ask))
    row("mid", lambda q: usd(q.mid))
    row("spread %", lambda q: pct(q.spread_pct, signed=False))
    row("top bid qty / makers", lambda q: f"{q.bid_qty} / {n(q.makers_bid)}")
    row("top ask qty / makers", lambda q: f"{q.ask_qty} / {n(q.makers_ask)}")
    row("depth units bid / ask", lambda q: f"{q.bid_depth_units} / {q.ask_depth_units}")
    row("imbalance %", lambda q: pct(q.imbalance_pct))
    row("top bid resting since", lambda q: ts(q.top_bid_resting_since))
    row("top ask resting since", lambda q: ts(q.top_ask_resting_since))
    row("last trade", lambda q: f"{usd(q.last_trade_price)} @ {ts(q.last_trade_at)}")
    row("24h vol / trades", lambda q: f"{n(q.vol_24h)} / {n(q.trade_count_24h)}")
    row("30d units / trades", lambda q: f"{n(q.units_30d)} / {n(q.trades_30d)}")
    row("30d VWAP", lambda q: usd(q.vwap_30d))
    row("30d price chg", lambda q: pct(q.price_change_30d_pct))
    row("7d candles o→c (vwap)", lambda q: _cs(q.candles_7d))
    row("30d candles o→c (vwap)", lambda q: _cs(q.candles_30d))
    row("holders", lambda q: n(q.holder_count))
    row("fair value (TPX model)", lambda q: (usd(q.fair_value) if q.fair_value is not None else ("no FV" if q.venue.startswith("TPX") else "n/a")))
    row("source", lambda q: q.source)
    row("fetched", lambda q: ts(q.fetched_at))
    return t


def _cs(c) -> str:
    if c is None:
        return "—"
    return f"{usd(c.open_cents)}→{usd(c.close_cents)} ({pct(c.change_pct)}) vwap {usd(c.vwap_cents)} n={c.bars}"


def arb_table(arb: TakerArb) -> Table:
    t = Table(title=arb.label, show_lines=False)
    t.add_column("size", justify="right")
    t.add_column("filled", justify="right")
    t.add_column("cost (all-in + ship)", justify="right")
    t.add_column("proceeds (all-in − ship)", justify="right")
    t.add_column("edge $", justify="right")
    t.add_column("edge / unit", justify="right")
    t.add_column("% of capital", justify="right")
    for f in arb.fills:
        t.add_row(str(f.size), str(f.filled), usd(f.cost_cents), usd(f.proceeds_cents),
                  colour_edge(f.edge_cents), colour_edge(f.edge_per_unit_cents), pct(f.edge_pct))
    if not arb.fills:
        t.add_row("—", "0", "—", "—", "—", "—", "—")
    cap = (f"top of book: buy {usd(arb.buy_clean_cents)} clean on {arb.buy_venue} → "
           f"sell {usd(arb.sell_clean_cents)} clean on {arb.sell_venue}; max size {arb.max_size}")
    t.caption = cap + ("; " + "; ".join(arb.notes) if arb.notes else "")
    return t


def maker_table(makers: Iterable[MakerLevel], margin: Decimal) -> Table:
    t = Table(title=f"Maker levels that still clear {margin * 100:.1f}% on capital", show_lines=False)
    t.add_column("rest where / side")
    t.add_column("rest at (clean)", justify="right")
    t.add_column("vs current top", justify="right")
    t.add_column("best-in-book?", justify="center")
    t.add_column("against", justify="right")
    t.add_column("edge / unit", justify="right")
    t.add_column("%", justify="right")
    t.add_column("notes")
    for m in makers:
        vs = "—" if m.improves_by_ticks is None else (f"{m.improves_by_ticks:+d} ticks" if m.current_top_cents is not None else "empty side")
        best = "—" if m.would_be_best is None else ("[green]yes[/green]" if m.would_be_best else "[yellow]no[/yellow]")
        t.add_row(m.label, usd(m.rest_clean_cents), f"{usd(m.current_top_cents)} ({vs})", best,
                  f"{usd(m.counter_clean_cents)} on {m.counter_venue}", colour_edge(m.expected_edge_cents),
                  pct(m.expected_edge_pct), "; ".join(m.notes))
    return t


def fv_liq_panel(a: Assessment) -> Panel:
    fv = a.fv
    lines = []
    if fv.has_fv:
        lines.append(f"[bold]Fair value[/bold] {usd(fv.fair_value_cents)} (model {fv.model_version or '?'})  "
                     f"TPX mid {pct(fv.tpx_mid_gap_pct)}  TPX bid {pct(fv.tpx_bid_gap_pct)}  TPX ask {pct(fv.tpx_ask_gap_pct)}  "
                     f"DN bid {pct(fv.dn_bid_gap_pct)}  DN ask {pct(fv.dn_ask_gap_pct)}")
    else:
        lines.append("[bold]Fair value[/bold] no FV (unpriced by TPX model)")
    if fv.flags:
        lines.append("  flags: " + ", ".join(f"[yellow]{f}[/yellow]" for f in fv.flags))
    for li in (a.tpx_liq, a.dn_liq):
        if li is None:
            continue
        lines.append(f"[bold]{li.venue}[/bold] spread {pct(li.spread_pct, signed=False)}  depth {li.bid_units}/{li.ask_units}  "
                     f"imb {pct(li.imbalance_pct)}  top age bid {_days(li.top_bid_age_days)} / ask {_days(li.top_ask_age_days)}  "
                     f"30d {n(li.units_30d)}u/{n(li.trades_30d)}t  holders {n(li.holder_count)}  liq factor {li.factor}")
        if li.flags:
            lines.append("  flags: " + ", ".join(f"[red]{f}[/red]" for f in li.flags))
    lines.append(f"[bold]Risk-adjusted score[/bold] {a.risk_adjusted_score}  (edge/unit × min(size,{a.size_cap}) × liq factors)")
    return Panel("\n".join(lines), title=f"{a.tpx_market}{' (vaulted)' if a.tpx_is_vaulted else ''}")


def _days(d: Optional[Decimal]) -> str:
    return "—" if d is None else f"{d:.1f}d"


def summary_table(ranked: Sequence[Assessment]) -> Table:
    t = Table(title="Ranked edge summary (taker legs unless noted)", show_lines=False)
    for c in ("#", "product", "TPX market", "best arb", "edge/unit", "%", "size", "score", "FV gap (DN ask)", "flags"):
        t.add_column(c, justify="right" if c in ("#", "edge/unit", "%", "size", "score") else "left")
    for i, a in enumerate(ranked, 1):
        arb = a.best_arb
        flags = a.tpx_liq.flags + (a.dn_liq.flags if a.dn_liq else []) + a.fv.flags
        t.add_row(str(i), a.product_name[:48], a.tpx_market + (" (vault)" if a.tpx_is_vaulted else ""),
                  (arb.label.split(":")[0] if arb else "—"),
                  colour_edge(arb.edge_per_unit_cents) if arb else "—",
                  pct(arb.edge_pct) if arb else "—",
                  str(arb.max_size) if arb else "—",
                  str(a.risk_adjusted_score),
                  pct(a.fv.dn_ask_gap_pct) if a.fv.has_fv else "no FV",
                  ", ".join(flags)[:60])
    return t


def print_assessment(console: Console, a: Assessment, margin: Decimal) -> None:
    for arb in (a.arb_a, a.arb_b):
        if arb:
            console.print(arb_table(arb))
    if a.makers:
        console.print(maker_table(a.makers, margin))
    console.print(fv_liq_panel(a))


# --- export ----------------------------------------------------------------
def _json_default(o: Any):
    if isinstance(o, Decimal):
        return str(o)
    if isinstance(o, datetime):
        return o.isoformat()
    if is_dataclass(o):
        return asdict(o)
    if hasattr(o, "model_dump"):
        return o.model_dump()
    return str(o)


def assessment_to_dict(a: Assessment) -> dict:
    d = {
        "product": a.product_name,
        "tpx_market": a.tpx_market,
        "tpx_is_vaulted": a.tpx_is_vaulted,
        "price_basis": "clean cents unless field says all_in",
        "tpx_quote": a.tpx.model_dump(exclude={"raw"}),
        "dealernet_quote": a.dn.model_dump(exclude={"raw"}) if a.dn else None,
        "arb_a_buy_dn_sell_tpx": _arb(a.arb_a),
        "arb_b_buy_tpx_sell_dn": _arb(a.arb_b),
        "maker_levels": [asdict(m) for m in a.makers],
        "fair_value": asdict(a.fv),
        "liquidity_tpx": asdict(a.tpx_liq),
        "liquidity_dealernet": asdict(a.dn_liq) if a.dn_liq else None,
        "risk_adjusted_score": a.risk_adjusted_score,
    }
    return json.loads(json.dumps(d, default=_json_default))


def _arb(arb: Optional[TakerArb]) -> Optional[dict]:
    if arb is None:
        return None
    d = asdict(arb)
    d["edge_per_unit_cents_all_in"] = arb.edge_per_unit_cents
    d["edge_pct_of_capital"] = arb.edge_pct
    for f, fill in zip(d["fills"], arb.fills):
        f["edge_cents"] = fill.edge_cents
        f["edge_pct"] = fill.edge_pct
    return d


SUMMARY_COLUMNS = [
    "rank", "product", "tpx_market", "tpx_is_vaulted", "best_arb", "edge_per_unit_usd_all_in", "edge_pct_of_capital",
    "max_size", "risk_adjusted_score", "tpx_best_bid_clean_usd", "tpx_best_ask_clean_usd", "dn_best_bid_usd",
    "dn_best_ask_usd", "fair_value_usd", "dn_ask_vs_fv_pct", "dn_bid_vs_fv_pct", "tpx_mid_vs_fv_pct",
    "tpx_units_30d", "tpx_trades_30d", "tpx_spread_pct", "flags", "tpx_fetched_at", "dn_fetched_at",
]


def summary_rows(ranked: Sequence[Assessment]) -> list[dict]:
    rows = []
    for i, a in enumerate(ranked, 1):
        arb = a.best_arb
        rows.append({
            "rank": i,
            "product": a.product_name,
            "tpx_market": a.tpx_market,
            "tpx_is_vaulted": a.tpx_is_vaulted,
            "best_arb": arb.label if arb else "",
            "edge_per_unit_usd_all_in": _d(arb.edge_per_unit_cents) if arb else "",
            "edge_pct_of_capital": _p(arb.edge_pct) if arb else "",
            "max_size": arb.max_size if arb else "",
            "risk_adjusted_score": str(a.risk_adjusted_score),
            "tpx_best_bid_clean_usd": _d(a.tpx.best_bid),
            "tpx_best_ask_clean_usd": _d(a.tpx.best_ask),
            "dn_best_bid_usd": _d(a.dn.best_bid) if a.dn else "",
            "dn_best_ask_usd": _d(a.dn.best_ask) if a.dn else "",
            "fair_value_usd": _d(a.fv.fair_value_cents) if a.fv.has_fv else "no FV",
            "dn_ask_vs_fv_pct": _p(a.fv.dn_ask_gap_pct),
            "dn_bid_vs_fv_pct": _p(a.fv.dn_bid_gap_pct),
            "tpx_mid_vs_fv_pct": _p(a.fv.tpx_mid_gap_pct),
            "tpx_units_30d": a.tpx.units_30d if a.tpx.units_30d is not None else "",
            "tpx_trades_30d": a.tpx.trades_30d if a.tpx.trades_30d is not None else "",
            "tpx_spread_pct": _p(a.tpx.spread_pct),
            "flags": "; ".join(a.tpx_liq.flags + (a.dn_liq.flags if a.dn_liq else []) + a.fv.flags),
            "tpx_fetched_at": a.tpx.fetched_at.isoformat(),
            "dn_fetched_at": a.dn.fetched_at.isoformat() if a.dn else "",
        })
    return rows


def _d(c) -> str:
    return "" if c is None else f"{Decimal(c) / 100:.2f}"


def _p(v) -> str:
    return "" if v is None else f"{Decimal(v):.2f}"


def export_csv(ranked: Sequence[Assessment], path: Path) -> None:
    rows = summary_rows(ranked)
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=SUMMARY_COLUMNS)
    w.writeheader()
    w.writerows(rows)
    Path(path).write_text(buf.getvalue())


def export_json(ranked: Sequence[Assessment], path: Path, meta: Optional[dict] = None) -> None:
    doc = {"generated_at": datetime.now(timezone.utc).isoformat(), "meta": meta or {},
           "assessments": [assessment_to_dict(a) for a in ranked]}
    Path(path).write_text(json.dumps(doc, indent=2, default=_json_default))


def export_markdown(ranked: Sequence[Assessment], path: Path) -> None:
    rows = summary_rows(ranked)
    cols = ["rank", "product", "tpx_market", "best_arb", "edge_per_unit_usd_all_in", "edge_pct_of_capital",
            "max_size", "risk_adjusted_score", "fair_value_usd", "dn_ask_vs_fv_pct", "flags"]
    out = ["| " + " | ".join(cols) + " |", "|" + "---|" * len(cols)]
    for r in rows:
        out.append("| " + " | ".join(str(r[c]).replace("|", "/") for c in cols) + " |")
    Path(path).write_text("\n".join(out) + "\n")
