"""Product-name normalisation and fuzzy matching between TPX and Dealernet.

Names on the two venues differ in word order, abbreviations ("HTA", "Hobby
Jumbo"), and punctuation. We extract structured parts (year, manufacturer,
brand, sport/game, format) and combine a token-set fuzzy score with hard
penalties when year or format disagree. Explicit overrides live in
mappings.yaml and always win.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Optional

import yaml
from rapidfuzz import fuzz

MANUFACTURERS = {
    "topps": "topps", "panini": "panini", "upper deck": "upper deck", "ud": "upper deck",
    "bowman": "topps", "leaf": "leaf", "fleer": "fleer", "donruss": "panini",
    "pokemon": "pokemon", "pokémon": "pokemon", "wizards": "wotc", "wotc": "wotc", "konami": "konami",
    "bandai": "bandai", "disney lorcana": "ravensburger", "ravensburger": "ravensburger",
}
SPORTS = {
    "baseball": "baseball", "mlb": "baseball", "basketball": "basketball", "nba": "basketball",
    "football": "football", "nfl": "football", "hockey": "hockey", "nhl": "hockey", "soccer": "soccer",
    "ufc": "ufc", "wrestling": "wrestling", "wwe": "wrestling", "f1": "f1", "golf": "golf",
    "pokemon": "pokemon", "pokémon": "pokemon", "magic": "mtg", "mtg": "mtg", "yu-gi-oh": "yugioh",
    "yugioh": "yugioh", "lorcana": "lorcana", "one piece": "one piece", "flesh and blood": "fab",
}
FORMATS = [
    (r"\bhobby\s*jumbo\b|\bjumbo\b|\bhta\b", "jumbo"),
    (r"\bhobby\b", "hobby"),
    (r"\bbreaker'?s?\s*delight\b", "breakers delight"),
    (r"\bfirst\s*off\s*the\s*line\b|\bfotl\b", "fotl"),
    (r"\bchoice\b", "choice"),
    (r"\bmega\b", "mega"),
    (r"\bblaster\b", "blaster"),
    (r"\bhanger\b", "hanger"),
    (r"\bcello\b|\bfat\s*pack\b|\bvalue\s*pack\b", "cello"),
    (r"\bretail\b", "retail"),
    (r"\belite\s*trainer\s*box\b|\betb\b", "etb"),
    (r"\bbooster\s*(bundle)\b", "booster bundle"),
    (r"\bbooster\s*(box|display)\b|\bdisplay\b", "booster box"),
    (r"\bcollector\s*booster\b", "collector booster"),
    (r"\bset\s*booster\b", "set booster"),
    (r"\bdraft\s*booster\b", "draft booster"),
    (r"\bplay\s*booster\b", "play booster"),
    (r"\btin\b", "tin"),
    (r"\bbundle\b", "bundle"),
]
CASE_RE = re.compile(r"\b(\d+\s*-?\s*box\s*)?(case|cs)\b", re.I)
BOX_RE = re.compile(r"\bbox(es)?\b", re.I)
YEAR_RE = re.compile(r"\b((?:19|20)\d{2})(?:[-/](\d{2,4}))?\b")
SHORT_YEAR_RE = re.compile(r"^\s*'?(\d{2})\b")  # "24 Topps Chrome ..." -> 2024
ABBREV = {"bb": "baseball", "bk": "basketball", "fb": "football", "hk": "hockey", "sv": "scarlet violet",
          "s&v": "scarlet violet", "sw": "sword shield", "swsh": "sword shield", "ygo": "yugioh", "pkmn": "pokemon"}
NOISE = re.compile(r"\b(factory\s*)?sealed\b|\bbrand\s*new\b|\bnew\b|\bbox(es)?\b|\bpack(s)?\b|\bcards?\b|\bthe\b", re.I)


@dataclass
class Normalized:
    original: str
    year: Optional[int] = None
    manufacturer: Optional[str] = None
    sport: Optional[str] = None
    format: Optional[str] = None
    is_case: bool = False
    tokens: str = ""
    extra: dict = field(default_factory=dict)

    @property
    def key(self) -> str:
        parts = [str(self.year or ""), self.manufacturer or "", self.sport or "", self.tokens,
                 self.format or "", "case" if self.is_case else "box"]
        return " ".join(p for p in parts if p).strip()


def normalize(name: str) -> Normalized:
    n = Normalized(original=name)
    s = name.lower().replace("&", " and ").replace("'", "")
    s = re.sub(r"[^\w\s\-/]", " ", s)
    for k, v in ABBREV.items():
        s = re.sub(rf"\b{re.escape(k)}\b", v, s)
    m = YEAR_RE.search(s)
    if m:
        n.year = int(m.group(1))
        s = s.replace(m.group(0), " ")
    else:
        m2 = SHORT_YEAR_RE.match(s)
        if m2:
            n.year = 2000 + int(m2.group(1))
            s = s[m2.end():]
    for k, v in MANUFACTURERS.items():
        if re.search(rf"\b{re.escape(k)}\b", s):
            n.manufacturer = v
            s = re.sub(rf"\b{re.escape(k)}\b", " ", s)
            break
    for k, v in SPORTS.items():
        if re.search(rf"\b{re.escape(k)}\b", s):
            n.sport = v
            s = re.sub(rf"\b{re.escape(k)}\b", " ", s)
            break
    n.is_case = bool(CASE_RE.search(s))
    s = CASE_RE.sub(" ", s)
    for pat, fmt in FORMATS:
        if re.search(pat, s):
            n.format = fmt
            s = re.sub(pat, " ", s)
            break
    s = NOISE.sub(" ", s)
    n.tokens = " ".join(sorted(set(t for t in re.split(r"[\s\-/]+", s) if t and len(t) > 1)))
    return n


def score(a: str, b: str) -> float:
    """0..100. Fuzzy token-set similarity with hard penalties for structured mismatches."""
    na, nb = normalize(a), normalize(b)
    base = fuzz.token_set_ratio(na.tokens or na.original.lower(), nb.tokens or nb.original.lower())
    s = float(base)
    if na.year and nb.year and na.year != nb.year:
        s -= 40
    if na.manufacturer and nb.manufacturer and na.manufacturer != nb.manufacturer:
        s -= 30
    if na.sport and nb.sport and na.sport != nb.sport:
        s -= 30
    if na.format and nb.format and na.format != nb.format:
        s -= 50  # hobby vs jumbo vs blaster are different products
    elif (na.format is None) != (nb.format is None):
        s -= 5
    if na.is_case != nb.is_case:
        s -= 45  # a case and a box are never the same product
    if na.year and nb.year and na.year == nb.year:
        s += 5
    if na.format and na.format == nb.format:
        s += 5
    return max(0.0, min(100.0, s))


def best_match(query: str, candidates: Iterable[str], threshold: float = 70.0) -> Optional[tuple[str, float]]:
    best: Optional[tuple[str, float]] = None
    for c in candidates:
        sc = score(query, c)
        if best is None or sc > best[1]:
            best = (c, sc)
    if best and best[1] >= threshold:
        return best
    return None


class Mappings:
    """Persisted TPX <-> Dealernet overrides.

    mappings.yaml:
        products:
          - tpx_id: "cfg_123"
            tpx_name: "2024 Topps Chrome Baseball Hobby Box"
            dealernet_name: "24 Topps Chrome BB Hobby"
            dealernet_url: "https://www.dealernetx.com/..."   # optional
    """

    def __init__(self, path: Path):
        self.path = Path(path)
        self.data: dict = {"products": []}
        if self.path.exists():
            loaded = yaml.safe_load(self.path.read_text()) or {}
            if isinstance(loaded, dict) and isinstance(loaded.get("products"), list):
                self.data = loaded

    @property
    def products(self) -> list[dict]:
        return self.data.setdefault("products", [])

    def dealernet_name_for(self, tpx_id: str) -> Optional[str]:
        for p in self.products:
            if str(p.get("tpx_id")) == str(tpx_id):
                return p.get("dealernet_name")
        return None

    def tpx_id_for_dealernet(self, dn_name: str) -> Optional[str]:
        for p in self.products:
            if (p.get("dealernet_name") or "").strip().lower() == dn_name.strip().lower():
                return str(p.get("tpx_id"))
        return None

    def set(self, tpx_id: str, tpx_name: str, dealernet_name: str, url: Optional[str] = None) -> None:
        for p in self.products:
            if str(p.get("tpx_id")) == str(tpx_id):
                p.update({"tpx_name": tpx_name, "dealernet_name": dealernet_name})
                if url:
                    p["dealernet_url"] = url
                break
        else:
            entry = {"tpx_id": tpx_id, "tpx_name": tpx_name, "dealernet_name": dealernet_name}
            if url:
                entry["dealernet_url"] = url
            self.products.append(entry)
        self.save()

    def remove(self, tpx_id: str) -> bool:
        before = len(self.products)
        self.data["products"] = [p for p in self.products if str(p.get("tpx_id")) != str(tpx_id)]
        self.save()
        return len(self.products) < before

    def save(self) -> None:
        self.path.write_text(yaml.safe_dump(self.data, sort_keys=False, allow_unicode=True))


def resolve_dealernet_name(tpx_id: str, tpx_name: str, candidates: Iterable[str], mappings: Optional[Mappings],
                           threshold: float = 70.0) -> Optional[tuple[str, float, str]]:
    """-> (dealernet_name, score, how) using mappings first, then fuzzy."""
    if mappings:
        m = mappings.dealernet_name_for(tpx_id)
        if m:
            return m, 100.0, "mappings.yaml"
    bm = best_match(tpx_name, candidates, threshold)
    if bm:
        return bm[0], bm[1], "fuzzy"
    return None
