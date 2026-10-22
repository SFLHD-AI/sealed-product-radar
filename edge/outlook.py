"""Where a product's market looks to be heading, from its own past prices.

Plain statistics over the Tradepost daily series (closes, VWAP, units): a 30-day
trend line, short-term momentum, volatility, the volume trend, where the
price sits in its 30-day range, plus what the two books say today. The
output is a label (Rising / Falling / Sideways), a confidence, the reasons in
plain words, and a 7-day range that assumes the recent drift and volatility
continue. It is a description of the past, not advice; the page says so.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class Outlook:
    label: str  # "Rising" | "Falling" | "Sideways" | "Not enough data"
    confidence: str  # "Low" | "Medium" | "High"
    score: float  # -1..+1, the net lean
    reasons: list[str] = field(default_factory=list)
    range_low_cents: Optional[int] = None  # 7-day band
    range_high_cents: Optional[int] = None
    trend_pct_per_week: Optional[float] = None
    volatility_pct: Optional[float] = None  # typical daily move
    momentum_pct: Optional[float] = None  # last vs 7-day VWAP
    volume_trend_pct: Optional[float] = None  # last 7 days vs the 23 before, per day
    range_position: Optional[float] = None  # 0 = at 30d low, 1 = at 30d high

    def as_dict(self) -> dict:
        return {k: getattr(self, k) for k in self.__dataclass_fields__}


def _slope(ys: list[float]) -> float:
    """Least-squares slope per step, as a fraction of the mean level."""
    n = len(ys)
    if n < 2:
        return 0.0
    xs = list(range(n))
    mx, my = sum(xs) / n, sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs) or 1.0
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    return (sxy / sxx) / my if my else 0.0


def analyze(closes: list[dict], *, tpx_bid_units: int = 0, tpx_ask_units: int = 0,
            dn_bid_cents: Optional[int] = None, dn_ask_cents: Optional[int] = None,
            last_cents: Optional[int] = None) -> Outlook:
    pts = [(c.get("close"), c.get("vwap") or c.get("close"), c.get("volume") or 0)
           for c in closes if c.get("close")]
    if len(pts) < 5:
        return Outlook("Not enough data", "Low", 0.0, ["Fewer than five days of Tradepost trades in the last 30 days."])
    prices = [float(p) for p, _, _ in pts]
    vols = [float(v) for _, _, v in pts]
    vwaps = [float(w) for _, w, _ in pts]
    last = float(last_cents or prices[-1])
    reasons: list[str] = []
    score = 0.0

    # 1. Trend: slope of the 30-day line, as % per week.
    trend_wk = _slope(prices) * 7 * 100
    if abs(trend_wk) >= 1.5:
        score += 0.35 if trend_wk > 0 else -0.35
        reasons.append(f"30-day trend is {'up' if trend_wk > 0 else 'down'} about {abs(trend_wk):.1f}% per week.")
    else:
        reasons.append("30-day trend is flat (under 1.5% per week either way).")

    # 2. Momentum: last price vs the volume-weighted average of the last 7 days.
    w7 = [(w, v) for w, v in zip(vwaps[-7:], vols[-7:]) if v > 0]
    vwap7 = (sum(w * v for w, v in w7) / sum(v for _, v in w7)) if w7 else sum(vwaps[-7:]) / len(vwaps[-7:])
    mom = (last / vwap7 - 1) * 100 if vwap7 else 0.0
    if abs(mom) >= 2:
        score += 0.25 if mom > 0 else -0.25
        reasons.append(f"Last sale is {abs(mom):.1f}% {'above' if mom > 0 else 'below'} the 7-day average price.")

    # 3. Volume: last 7 days vs the rest of the window, per day.
    recent = sum(vols[-7:]) / max(len(vols[-7:]), 1)
    earlier = sum(vols[:-7]) / max(len(vols[:-7]), 1) if len(vols) > 7 else recent
    vol_trend = (recent / earlier - 1) * 100 if earlier else 0.0
    if earlier and abs(vol_trend) >= 25:
        rising_vol = vol_trend > 0
        # Rising volume confirms the direction the price is moving; fading volume weakens it.
        direction = 1 if (trend_wk + mom) > 0 else -1
        score += (0.15 if rising_vol else -0.1) * direction
        reasons.append(f"Trading volume is {'up' if rising_vol else 'down'} {abs(vol_trend):.0f}% this week versus earlier in the month"
                       + (", which backs the move." if rising_vol else ", so the move has less behind it."))

    # 4. Range position: near the 30-day high or low.
    lo, hi = min(prices), max(prices)
    pos = (last - lo) / (hi - lo) if hi > lo else 0.5
    if pos >= 0.9:
        reasons.append("Price is at the top of its 30-day range; moves from here have less room unless volume keeps rising.")
        score -= 0.05
    elif pos <= 0.1:
        reasons.append("Price is at the bottom of its 30-day range; sellers may be getting exhausted.")
        score += 0.05

    # 5. Today's books: Tradepost bid/ask depth and where Dealernet sits.
    if tpx_bid_units + tpx_ask_units >= 10:
        imb = (tpx_bid_units - tpx_ask_units) / (tpx_bid_units + tpx_ask_units)
        if abs(imb) >= 0.3:
            score += 0.15 if imb > 0 else -0.15
            reasons.append(f"Tradepost order book is {'buyer' if imb > 0 else 'seller'}-heavy: {tpx_bid_units:,} units bid vs {tpx_ask_units:,} offered.")
    if dn_bid_cents and last:
        prem = (dn_bid_cents / last - 1) * 100
        if prem >= 5:
            score += 0.1
            reasons.append(f"Dealernet is paying {prem:.0f}% above the last Tradepost sale, so Tradepost looks cheap to that market.")
        elif prem <= -5:
            score -= 0.1
            reasons.append(f"Dealernet is paying {abs(prem):.0f}% below the last Tradepost sale, so Tradepost looks rich to that market.")

    # 6. Volatility and the 7-day band.
    rets = [math.log(b / a) for a, b in zip(prices[:-1], prices[1:]) if a > 0 and b > 0]
    daily_vol = (sum(r * r for r in rets) / len(rets)) ** 0.5 * 100 if rets else 0.0
    drift = trend_wk / 100
    band = daily_vol / 100 * math.sqrt(7)
    lo7 = int(round(last * (1 + drift - band)))
    hi7 = int(round(last * (1 + drift + band)))
    if daily_vol >= 6:
        reasons.append(f"Choppy: typical daily move is {daily_vol:.1f}%, so expect a wide range.")

    score = max(-1.0, min(1.0, score))
    label = "Rising" if score >= 0.25 else "Falling" if score <= -0.25 else "Sideways"
    conf = "High" if abs(score) >= 0.6 and daily_vol < 6 else "Medium" if abs(score) >= 0.3 else "Low"
    return Outlook(label, conf, round(score, 2), reasons, lo7, hi7, round(trend_wk, 2), round(daily_vol, 2),
                   round(mom, 2), round(vol_trend, 1) if earlier else None, round(pos, 2))
