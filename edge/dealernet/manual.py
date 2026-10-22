"""Manual backend: bid/ask/qty from CLI flags, or interactive prompt."""
from __future__ import annotations

from typing import Optional

from rich.console import Console
from rich.prompt import IntPrompt, Prompt

from .base import DealernetLevel, DealernetQuote, DealernetSource, dollars_to_cents


class ManualSource(DealernetSource):
    name = "manual"

    def __init__(self, bid=None, ask=None, bid_qty: Optional[int] = None, ask_qty: Optional[int] = None,
                 qty: Optional[int] = None, interactive: bool = True, console: Optional[Console] = None):
        self.bid = bid
        self.ask = ask
        self.bid_qty = bid_qty if bid_qty is not None else qty
        self.ask_qty = ask_qty if ask_qty is not None else qty
        self.interactive = interactive
        self.console = console or Console(emoji=False)

    def get_quote(self, product: str, upc: Optional[str] = None) -> Optional[DealernetQuote]:
        bid, ask, bq, aq = self.bid, self.ask, self.bid_qty, self.ask_qty
        if bid is None and ask is None:
            if not self.interactive:
                return None
            self.console.print(f"[bold]Dealernet quote for[/bold] {product}  (blank = none)")
            b = Prompt.ask("  best BID $", default="")
            bid = b or None
            if bid:
                bq = IntPrompt.ask("  bid qty", default=1)
            a = Prompt.ask("  best ASK $", default="")
            ask = a or None
            if ask:
                aq = IntPrompt.ask("  ask qty", default=1)
        q = DealernetQuote(product=product, source="manual")
        if bid is not None:
            q.bids.append(DealernetLevel(price_cents=dollars_to_cents(bid), quantity=bq or 1))
        if ask is not None:
            q.asks.append(DealernetLevel(price_cents=dollars_to_cents(ask), quantity=aq or 1))
        return q
