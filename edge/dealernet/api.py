"""Dealernet API backend (DEALERNET_ACCESS=api).

Dealernet's terms mention API access for automated use, but the API is not
publicly documented. This backend is the switch for when you get a key: set
DEALERNET_API_KEY (and DEALERNET_API_URL if they give you a different base)
and DEALERNET_ACCESS=api. The request/response mapping is filled in from their
docs once the key arrives; until then get_quote says so instead of guessing.
Read-only: only quote lookups, never listings, offers or orders.
"""
from __future__ import annotations

from typing import Optional

from .base import DealernetQuote, DealernetSource


class ApiSource(DealernetSource):
    name = "api"

    def __init__(self, api_key: str, base_url: str):
        if not api_key:
            raise RuntimeError("DEALERNET_ACCESS=api but DEALERNET_API_KEY is not set.")
        self.base_url = base_url.rstrip("/")
        self._key = api_key

    def get_quote(self, product: str, upc: Optional[str] = None) -> Optional[DealernetQuote]:
        raise NotImplementedError(
            "Dealernet API key is set, but the endpoint mapping needs Dealernet's API docs. "
            "Share them and the quote lookup will be wired in; use DEALERNET_ACCESS=scrape until then."
        )
