"""Dealernet matching must agree with Tradepost on the UPC, not just the name."""
from pathlib import Path

from edge.dealernet.scrape import ScrapeSource


def _row(name, variation, upc, url):
    return (f"<tr><td data-label='Product'><a class='product-link' href='{url}'>{name}</a></td>"
            f"<td data-label='Variation'>{variation}</td><td data-label='UPC'>{upc}</td></tr>")


SEARCH = "<table>" + _row("Prismatic Evolutions Scarlet & Violet POKEMON Etb", "Elite Trainer", "196214105133", "listing.php?a=1") + \
    _row("Prismatic Evolutions Scarlet & Violet POKEMON Etb", "Elite Trainer (Center)", "196214105140", "listing.php?a=2") + "</table>"


def _listing(upc):
    return (f"<p><strong>UPC:</strong> {upc}</p>Current High Bid: <strong>$100.00</strong> Current Low Ask: <strong>$110.00</strong>")


class _Src(ScrapeSource):
    def __init__(self, tmp, pages):
        super().__init__("u", "p", tmp)
        self.pages = pages

    def _get(self, url):  # no network
        for key, page in self.pages.items():
            if key in url:
                return page
        return "<table></table>"


def test_upc_hit_wins_over_name(tmp_path):
    src = _Src(tmp_path, {"search.php": SEARCH, "a=2": _listing("196214105140"), "a=1": _listing("196214105133")})
    q = src.get_quote("Prismatic Evolutions Elite Trainer Box", upc="196214105140")
    assert q.url.endswith("a=2") and q.upc_match is True and q.bids


def test_different_upc_is_not_compared(tmp_path):
    src = _Src(tmp_path, {"search.php": SEARCH, "a=1": _listing("196214105133")})
    q = src.get_quote("Prismatic Evolutions Elite Trainer Box", upc="000000000000")
    assert q is not None and q.upc_match is False and not q.bids and not q.asks
    assert "Different product" in q.upc_note


def test_no_upc_falls_back_to_name(tmp_path):
    src = _Src(tmp_path, {"search.php": SEARCH, "a=1": _listing("196214105133")})
    q = src.get_quote("Prismatic Evolutions Elite Trainer Box")
    assert q is not None and q.upc_match is None and q.bids


def test_multi_upc_cell_and_leading_zero(tmp_path):
    search = "<table>" + _row("25/6 Topps NBA Chrome Update Basketball Mega V1", "", "887521161485 887521161478", "listing.php?a=3") + \
        _row("25/6 Topps NBA Chrome Update Basketball Mega V2", "Fanatics", "887521161522", "listing.php?a=4") + "</table>"
    src = _Src(tmp_path, {"search.php": search, "a=3": _listing("0887521161485"), "a=4": _listing("887521161522")})
    q = src.get_quote("2025/26 Topps Chrome Update Basketball Mega Box", upc="887521161485")
    assert q.url.endswith("a=3") and q.upc_match is True


def test_pokemon_center_and_knockout_names():
    from edge.dealernet.scrape import page_match_score as sc
    assert sc("30th Celebration Pokemon Center Elite Trainer Box", "30th Celebration POKEMON Etb Pkc") > \
        sc("30th Celebration Pokemon Center Elite Trainer Box", "30th Celebration POKEMON Etb")
    assert sc("30th Celebration Elite Trainer Box", "30th Celebration POKEMON Etb") > \
        sc("30th Celebration Elite Trainer Box", "30th Celebration POKEMON Etb Pkc")
    assert sc("30th Celebration Knock Out Collection", "30th Celebration POKEMON M4 Knockout") > \
        sc("30th Celebration Knock Out Collection", "30th Celebration POKEMON M1 Collection Box")
