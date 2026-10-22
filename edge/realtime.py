"""Realtime book sync (watch mode).

Protocol per the TPX docs summary: snapshot via getOrderBook (gives `sequence`),
then subscribe to `public:book:<configId>:<keyId>` (also `public:trades:…`,
`public:stats:…`). Drop pushes whose `seq` <= snapshot sequence, apply later
ones, and re-snapshot on a `RESYNC` message.

BookSync is the pure, unit-tested state machine. The socket transport needs the
socket URL + message framing from Integration -> Realtime Socket, which could
not be read from the build environment; set TPX_SOCKET_URL once known. Until
then `edge watch` polls getOrderBook on an interval, which exercises the same
BookSync path.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Optional


@dataclass
class BookSync:
    config_id: str
    key_id: Optional[str]
    sequence: int = -1
    bids: dict[int, dict] = field(default_factory=dict)  # price -> level
    asks: dict[int, dict] = field(default_factory=dict)
    needs_resync: bool = True
    dropped: int = 0
    applied: int = 0

    @property
    def channels(self) -> list[str]:
        k = self.key_id or "*"
        return [f"public:book:{self.config_id}:{k}", f"public:trades:{self.config_id}:{k}", f"public:stats:{self.config_id}:{k}"]

    def snapshot(self, book: dict) -> None:
        self.sequence = int(book.get("sequence") or 0)
        self.bids = {_price(l): l for l in book.get("bids") or []}
        self.asks = {_price(l): l for l in book.get("asks") or []}
        self.needs_resync = False

    def apply(self, msg: dict) -> bool:
        """Apply one push. Returns True if the book changed."""
        if str(msg.get("type", "")).upper() == "RESYNC":
            self.needs_resync = True
            return False
        seq = msg.get("seq")
        if seq is None or int(seq) <= self.sequence:
            self.dropped += 1
            return False
        if self.sequence >= 0 and int(seq) > self.sequence + 1 and msg.get("strict", False):
            self.needs_resync = True  # gap detected
            return False
        self.sequence = int(seq)
        for side, store in (("bids", self.bids), ("asks", self.asks)):
            for lv in msg.get(side) or []:
                p = _price(lv)
                if int(lv.get("quantity") or 0) <= 0:
                    store.pop(p, None)
                else:
                    store[p] = lv
        self.applied += 1
        return True

    def top(self) -> tuple[Optional[int], Optional[int]]:
        return (max(self.bids) if self.bids else None, min(self.asks) if self.asks else None)

    def as_book(self) -> dict:
        return {
            "sequence": self.sequence,
            "bids": sorted(self.bids.values(), key=lambda l: -_price(l)),
            "asks": sorted(self.asks.values(), key=lambda l: _price(l)),
        }


def _price(level: dict) -> int:
    """Accept raw TPX levels (priceCents) and BookLevel dumps (price_cents)."""
    v = level.get("priceCents", level.get("price_cents"))
    if v is None:
        raise KeyError("priceCents")
    return int(v)


class PollingFeed:
    """Fallback transport: re-snapshot on an interval. Same consumer API as a socket feed."""

    def __init__(self, fetch_snapshot: Callable[[], dict], sync: BookSync):
        self.fetch = fetch_snapshot
        self.sync = sync

    def tick(self) -> bool:
        before = self.sync.top(), self.sync.sequence
        self.sync.snapshot(self.fetch())
        return (self.sync.top(), self.sync.sequence) != before
