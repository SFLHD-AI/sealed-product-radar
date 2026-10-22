from datetime import timezone

import pytest

from edge.dealernet.csv_source import parse_dealernet_csv
from edge.dealernet.manual import ManualSource
from edge.matching import Mappings, best_match, normalize, resolve_dealernet_name, score
from edge.realtime import BookSync

LADDER = """product,side,price,qty,member,observed_at
2024 Topps Chrome Baseball Hobby Box,bid,245,3,m1829,2026-10-20
2024 Topps Chrome Baseball Hobby Box,bid,240,5,m0007,
2024 Topps Chrome Baseball Hobby Box,ask,$262,1,m0412,2026-10-20T18:00:00Z
Pokemon SV 151 Booster Bundle,ask,41.50,12,m99,
"""

WIDE = """product,bid,bid_qty,ask,ask_qty
2024 Topps Chrome Baseball Hobby Box,245,3,262,1
24 Topps Chrome BB Jumbo,,,470,2
"""


def test_parse_ladder_csv():
    q = parse_dealernet_csv(LADDER)
    tc = q["2024 Topps Chrome Baseball Hobby Box"]
    assert [l.price_cents for l in tc.bids] == [24500, 24000]
    assert tc.asks[0].price_cents == 26200 and tc.asks[0].member == "m0412"
    assert tc.asks[0].observed_at.tzinfo == timezone.utc
    assert q["Pokemon SV 151 Booster Bundle"].asks[0].price_cents == 4150
    vq = tc.to_venue_quote("cfg")
    assert vq.best_bid == 24500 and vq.best_ask == 26200 and vq.bid_depth_units == 8 and vq.makers_bid == 2


def test_parse_wide_csv():
    q = parse_dealernet_csv(WIDE)
    assert q["2024 Topps Chrome Baseball Hobby Box"].bids[0].quantity == 3
    jumbo = q["24 Topps Chrome BB Jumbo"]
    assert jumbo.bids == [] and jumbo.asks[0].price_cents == 47000


def test_bad_header_raises():
    with pytest.raises(ValueError):
        parse_dealernet_csv("foo,bar\n1,2\n")


def test_manual_source_flags():
    src = ManualSource(bid="252", ask=258.0, qty=3, interactive=False)
    q = src.get_quote("X")
    assert q.bids[0].price_cents == 25200 and q.asks[0].quantity == 3
    assert ManualSource(interactive=False).get_quote("X") is None


def test_normalize_extracts_structure():
    n = normalize("24 Topps Chrome BB Hobby Jumbo (HTA) 8-Box Case")
    assert (n.year, n.manufacturer, n.sport, n.format, n.is_case) == (2024, "topps", "baseball", "jumbo", True)


def test_scores_separate_formats_years_and_cases():
    base = "2024 Topps Chrome Baseball Hobby Box"
    assert score(base, "24 Topps Chrome BB Hobby") >= 90
    assert score(base, "2024 Topps Chrome Baseball Jumbo Box") < 70
    assert score(base, "2024 Topps Chrome Baseball Hobby 12-Box Case") < 70
    assert score(base, "2023 Topps Chrome Baseball Hobby Box") < 70
    assert best_match(base, ["2023 Topps Chrome Baseball Hobby Box", "24 Topps Chrome BB Hobby"])[0] == "24 Topps Chrome BB Hobby"


def test_mappings_override_wins(tmp_path):
    mp = Mappings(tmp_path / "m.yaml")
    mp.set("cfg_1", "2024 Topps Chrome Baseball Hobby Box", "TC24 HOBBY (weird DN name)")
    r = resolve_dealernet_name("cfg_1", "2024 Topps Chrome Baseball Hobby Box", ["anything"], mp)
    assert r == ("TC24 HOBBY (weird DN name)", 100.0, "mappings.yaml")
    assert Mappings(tmp_path / "m.yaml").tpx_id_for_dealernet("tc24 hobby (weird dn name)") == "cfg_1"
    assert mp.remove("cfg_1") and mp.products == []


def test_booksync_sequence_rules():
    s = BookSync("cfg", "key")
    s.snapshot({"sequence": "10", "bids": [{"priceCents": 100, "quantity": 1}], "asks": []})
    assert not s.apply({"seq": 9, "bids": [{"priceCents": 200, "quantity": 1}]})  # <= snapshot: dropped
    assert not s.apply({"seq": 10, "bids": [{"priceCents": 200, "quantity": 1}]})
    assert s.apply({"seq": 11, "bids": [{"priceCents": 200, "quantity": 2}], "asks": [{"priceCents": 300, "quantity": 1}]})
    assert s.top() == (200, 300)
    assert s.apply({"seq": 12, "bids": [{"priceCents": 200, "quantity": 0}]})  # qty 0 removes
    assert s.top() == (100, 300)
    assert s.dropped == 2 and s.applied == 2
    assert not s.apply({"type": "RESYNC"}) and s.needs_resync
    assert s.channels[0] == "public:book:cfg:key"
