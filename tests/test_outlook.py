"""Outlook: direction, confidence, range and plain-English reasons from a daily series."""
from edge.outlook import analyze


def _series(start, step, days=30, vol=10):
    return [{"t": f"2026-09-{d + 1:02d}", "close": int(start + step * d), "vwap": int(start + step * d), "volume": vol} for d in range(days)]


def test_rising_series_reads_rising():
    o = analyze(_series(10000, 60), tpx_bid_units=500, tpx_ask_units=50)
    assert o.label == "Rising" and o.score > 0
    assert o.range_low_cents < o.range_high_cents
    assert any("trend is up" in r for r in o.reasons) and any("buyer-heavy" in r for r in o.reasons)


def test_falling_series_with_dealernet_discount():
    o = analyze(_series(20000, -120), dn_bid_cents=12000)
    assert o.label == "Falling"
    assert any("below the last Tradepost sale" in r for r in o.reasons)


def test_flat_series_is_sideways_and_short_series_is_honest():
    assert analyze(_series(5000, 0)).label == "Sideways"
    short = analyze(_series(5000, 10, days=3))
    assert short.label == "Not enough data" and short.range_low_cents is None


def test_as_dict_is_json_friendly():
    import json
    json.dumps(analyze(_series(5000, 5)).as_dict())
