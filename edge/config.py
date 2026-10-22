"""Settings loaded from .env / environment (pydantic-settings).

The API key is a SecretStr and is never logged. Everything here can be
overridden per-run from CLI flags (see cli.py).
"""
from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import Literal, Optional

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

Vertical = Literal["sealed_wax", "sealed_tcg", "both"]
DealernetAccess = Literal["manual", "csv", "scrape", "api"]
DealernetPlan = Literal["basic", "vendor"]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore", case_sensitive=False
    )

    tpx_api_key: Optional[SecretStr] = Field(default=None, description="X-API-Key for TPX")
    tpx_graphql_url: str = "https://platform-api.tradepost.co/graphql"
    tpx_socket_url: Optional[str] = Field(
        default=None,
        description="Realtime socket URL (take it from TPX docs: Integration -> Realtime Socket)",
    )

    vertical: Vertical = "both"
    dealernet_access: DealernetAccess = "manual"
    dealernet_plan: DealernetPlan = "vendor"
    # DEALERNET_ACCESS=scrape logs in with these (read-only page reads).
    dealernet_username: Optional[str] = None
    dealernet_password: Optional[SecretStr] = None
    # DEALERNET_ACCESS=api uses an API key instead, once Dealernet issues one.
    dealernet_api_key: Optional[SecretStr] = None
    dealernet_api_url: str = "https://www.dealernetx.com/api"

    # TCGplayer market price as a reference (sealed TCG only). Read-only.
    tcgplayer_public_key: Optional[str] = None
    tcgplayer_private_key: Optional[SecretStr] = None
    tcgplayer_api_url: str = "https://api.tcgplayer.com"
    # Without API keys, read the public product page data (market price, last sale, trend).
    tcgplayer_pages: bool = True

    # Dealernet fee schedule: listed-price trades are free; name-your-price /
    # counter offers cost 3% on the Vendor plan. We model the *taker* (hit a
    # listed price) as 0 bps and the *maker* (name your price) as 300 bps.
    dealernet_listed_fee_bps: int = 0
    dealernet_named_price_fee_bps: int = 300

    shipping_box_usd: Decimal = Decimal("15")
    shipping_case_usd: Decimal = Decimal("45")
    handling_usd: Decimal = Decimal("0")

    # Master switch for anything that would act on a marketplace (list, buy,
    # offer, auto-alerts that trade). Off by default; no such feature exists
    # yet, and any future one must check this AND ask for confirmation.
    actions_enabled: bool = False

    target_margin: Decimal = Decimal("0.08")
    stale_days: int = 7
    thin_book_units: int = 3
    size_cap: int = 10  # units used in risk-adjusted ranking

    # Client behaviour
    rate_limit_rps: float = 4.0
    cache_ttl_seconds: float = 30.0
    request_timeout_seconds: float = 20.0
    max_retries: int = 4

    # Optional pinned asset type IDs (discovered by `edge doctor` / introspection)
    tpx_asset_type_sealed_wax: Optional[str] = None
    tpx_asset_type_sealed_tcg: Optional[str] = None

    cache_dir: Path = Path(".cache")
    mappings_path: Path = Path("mappings.yaml")
    fixtures_dir: Path = Path("tests/fixtures")

    def shipping_cents(self, is_case: bool) -> int:
        usd = (self.shipping_case_usd if is_case else self.shipping_box_usd) + self.handling_usd
        return int((usd * 100).quantize(Decimal("1")))

    def require_key(self) -> str:
        if not self.tpx_api_key or not self.tpx_api_key.get_secret_value():
            raise RuntimeError(
                "TPX_API_KEY is not set. Put TPX_API_KEY=tp_live_... in .env "
                "(terminal.tradepost.co -> Settings -> API Keys)."
            )
        return self.tpx_api_key.get_secret_value()


def load_settings(**overrides) -> Settings:
    return Settings(**{k: v for k, v in overrides.items() if v is not None})
