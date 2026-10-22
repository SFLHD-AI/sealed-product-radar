"""Orchestration shared by the CLI commands: resolve a product, fetch TPX
quotes per market, build the Dealernet quote, assess."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Optional

from .config import Settings
from .dealernet.base import DealernetQuote, DealernetSource, dealernet_cost_model
from .edge import Assessment, assess
from .matching import Mappings, best_match, resolve_dealernet_name, score
from .models import CostModel, Product, VenueQuote
from .tpx.catalog import Catalog
from .tpx.client import FixtureRecorder, TPXClient
from .tcgplayer import TCGplayerPages, TCGplayerClient, TCGplayerIdMap, TCGPrice
from .tpx.market import MarketData

log = logging.getLogger("edge.service")


@dataclass
class ProductQuotes:
    product: Product
    tpx: list[tuple[VenueQuote, CostModel]]
    dn: Optional[VenueQuote]
    dn_cm: Optional[CostModel]
    dn_match: Optional[str]  # how the Dealernet product was matched


class EdgeService:
    def __init__(self, settings: Settings, *, record_fixtures: bool = False, client: Optional[TPXClient] = None,
                 demo_fixtures: Optional[Path] = None):
        self.settings = settings
        recorder = FixtureRecorder(settings.fixtures_dir) if record_fixtures else None
        transport = None
        api_key = None
        if demo_fixtures is not None:
            from .tpx.mock import mock_transport

            transport = mock_transport(demo_fixtures)
            api_key = "demo"
        self.client = client or TPXClient(
            api_key or settings.require_key(),
            settings.tpx_graphql_url,
            transport=transport,
            rate_limit_rps=settings.rate_limit_rps,
            cache_ttl_s=settings.cache_ttl_seconds,
            timeout_s=settings.request_timeout_seconds,
            max_retries=settings.max_retries,
            recorder=recorder,
        )
        # Demo IDs (at_sealed_wax, ...) must never land in the live ID cache.
        cache_dir = settings.cache_dir / "demo" if demo_fixtures is not None else settings.cache_dir
        self.catalog = Catalog(
            self.client,
            cache_dir,
            {"sealed_wax": settings.tpx_asset_type_sealed_wax, "sealed_tcg": settings.tpx_asset_type_sealed_tcg},
        )
        self.market = MarketData(self.client)
        self.mappings = Mappings(settings.mappings_path)
        self.tcg: Optional[TCGplayerClient] = None
        self.tcg_pages: Optional[TCGplayerPages] = None
        self.tcg_ids = TCGplayerIdMap(self.client, settings.cache_dir)
        if settings.tcgplayer_public_key and settings.tcgplayer_private_key:
            self.tcg = TCGplayerClient(settings.tcgplayer_public_key, settings.tcgplayer_private_key.get_secret_value(),
                                       settings.tcgplayer_api_url, settings.cache_dir)
        if settings.tcgplayer_pages:  # no keys: read the public product page data instead
            self.tcg_pages = TCGplayerPages(settings.cache_dir)

    def close(self) -> None:
        self.client.close()
        if self.tcg:
            self.tcg.close()
        if self.tcg_pages:
            self.tcg_pages.close()

    # --- TCGplayer reference ----------------------------------------------
    def tcgplayer_price(self, product: Product, tcgplayer_id: Optional[str] = None) -> Optional[TCGPrice]:
        """TCGplayer market/low for the product, or None (no keys, no mapping, unpriced, or API down)."""
        if self.tcg is None and self.tcg_pages is None:
            return None
        pid = tcgplayer_id or self.tcg_ids.get(product.asset_configuration_id)
        if not pid:
            return None
        try:
            if self.tcg is not None:
                api = self.tcg.prices([pid]).get(str(pid))
                if api and self.tcg_pages is None:
                    return api
            else:
                api = None
            page = self.tcg_pages.price(pid) if self.tcg_pages is not None else None
            if api and page:  # keys give the market price; the page adds the last sale and trend
                api.last_sale_cents, api.last_sale_at, api.trend, api.trend_pct = page.last_sale_cents, page.last_sale_at, page.trend, page.trend_pct
                return api
            return api or page
        except Exception as exc:  # noqa: BLE001 - a reference price must never break the compare
            log.warning("TCGplayer price unavailable for %s: %s", product.name, exc)
            return None

    # --- products ---------------------------------------------------------
    def search(self, text: str, limit: int = 10, *, enrich: bool = False) -> list[Product]:
        products = self.catalog.search(text, self.settings.vertical, limit)
        # Rank by our own matcher so the obvious hit is first.
        products.sort(key=lambda p: -score(text, p.name))
        for p in products:
            try:
                p.key_id = self.catalog.resolve_condition_key(p.asset_type_id, "MINT")
            except Exception as exc:  # noqa: BLE001
                log.warning("MINT key unresolved for %s: %s", p.name, exc)
        if enrich:
            for p in products:
                p.raw["has_mint_book"] = None
                p.raw["fair_value_cents"] = None
                try:
                    book = self.market.order_book(p, None, levels=1)
                    p.raw["has_mint_book"] = bool((book.get("bids") or book.get("asks")))
                    p.raw["top_bid"] = (book.get("summary") or {}).get("topBidCents")
                    p.raw["top_ask"] = (book.get("summary") or {}).get("topAskCents")
                except Exception as exc:  # noqa: BLE001
                    p.raw["has_mint_book"] = f"error: {exc}"
                try:
                    fv, model = self.market.fair_value(p)
                    p.raw["fair_value_cents"] = fv
                    p.raw["fair_value_model"] = model
                except Exception as exc:  # noqa: BLE001
                    p.raw["fair_value_cents"] = f"error: {exc}"
        return products

    def resolve_product(self, text: str, *, pick: Optional[int] = None, exact_id: Optional[str] = None) -> Product:
        if exact_id:
            hits = [p for p in self.search(text) if p.asset_configuration_id == exact_id]
            if hits:
                return hits[0]
            raise LookupError(f"assetConfigurationId {exact_id} not among search results for {text!r}")
        mapped = self.mappings.tpx_id_for_dealernet(text)
        results = self.search(text)
        if not results:
            raise LookupError(f"No TPX product matches {text!r}")
        if mapped:
            for p in results:
                if p.asset_configuration_id == mapped:
                    return p
        if pick is not None:
            return results[pick]
        top = results[0]
        if len(results) > 1 and score(text, results[1].name) >= score(text, top.name) - 2:
            log.warning("Ambiguous match for %r: %s vs %s (use --pick or --id)", text, top.name, results[1].name)
        return top

    # --- cost models ------------------------------------------------------
    def dealernet_cost_model(self, product: Product, shipping_override_cents: Optional[int] = None) -> CostModel:
        ship = shipping_override_cents if shipping_override_cents is not None else self.settings.shipping_cents(product.is_case)
        s = self.settings
        return dealernet_cost_model(s.dealernet_plan, s.dealernet_listed_fee_bps, s.dealernet_named_price_fee_bps, ship, ship)

    # --- quotes -----------------------------------------------------------
    def tpx_quotes(self, product: Product, shipping_override_cents: Optional[int] = None,
                   *, with_candles: bool = True) -> list[tuple[VenueQuote, CostModel]]:
        ship = shipping_override_cents if shipping_override_cents is not None else self.settings.shipping_cents(product.is_case)
        out: list[tuple[VenueQuote, CostModel]] = []
        markets = self.market.product_markets(product)
        if not markets:
            log.warning("No TPX markets for %s", product.name)
        fv, model = self.market.fair_value(product)
        for m in markets:
            if m.get("tradingState") and str(m["tradingState"]).upper() not in ("OPEN", "ACTIVE", "TRADING"):
                log.info("market %s state %s", m.get("displayName"), m.get("tradingState"))
            q = self.market.venue_quote(product, m, with_candles=with_candles)
            q.fair_value, q.fair_value_model = fv, model
            q.raw["product_meta"] = self._product_meta(product)
            cm = self.market.market_cost_model(str(m["id"]), m.get("displayName") or str(m["id"]), ship, ship, bool(m.get("isVaulted")))
            out.append((q, cm))
        return out

    def _product_meta(self, product: Product) -> dict:
        """What the dashboard shows on a tile: group, set, format, picture."""
        from .images import image_data_uri

        data = product.raw.get("data") or {}
        variants = data.get("image_variants") or {}
        url = variants.get("200_x_200") or (data.get("image_urls") or [None])[0]
        return {
            "asset_type": product.asset_type_name,
            "brand": product.brand or data.get("brand"),
            "set": product.raw.get("assetName"),
            "format": product.format,
            "is_case": product.is_case,
            "year": product.year,
            "upc": data.get("upc"),
            "image": image_data_uri(url, self.settings.cache_dir) if url else None,
        }

    def dealernet_quote(self, product: Product, source: DealernetSource, dn_name: Optional[str] = None,
                        candidates: Optional[list[str]] = None) -> tuple[Optional[VenueQuote], Optional[str]]:
        name = dn_name
        how = "explicit --dn-name" if dn_name else None
        if name is None and candidates:
            r = resolve_dealernet_name(product.asset_configuration_id, product.name, candidates, self.mappings)
            if r:
                name, sc, how = r[0], r[1], f"{r[2]} ({r[1]:.0f})"
        if name is None:
            name = self.mappings.dealernet_name_for(product.asset_configuration_id) or product.name
            how = how or ("mappings.yaml" if self.mappings.dealernet_name_for(product.asset_configuration_id) else "tpx name")
        upc = str((product.raw.get("data") or {}).get("upc") or "").strip() or None
        dq: Optional[DealernetQuote] = source.get_quote(name, upc)
        if dq is not None and dq.upc_match is False:
            how = f"{how}; UPC mismatch"
        if dq is None:
            return None, how
        return dq.to_venue_quote(product.asset_configuration_id, product.name), how

    # --- full compare -----------------------------------------------------
    def compare(self, product: Product, dn_source: Optional[DealernetSource], *, margin: Optional[Decimal] = None,
                shipping_cents: Optional[int] = None, dn_name: Optional[str] = None,
                dn_candidates: Optional[list[str]] = None, with_candles: bool = True,
                tcgplayer_id: Optional[str] = None) -> tuple[ProductQuotes, list[Assessment]]:
        s = self.settings
        tpx = self.tpx_quotes(product, shipping_cents, with_candles=with_candles)
        tcg = self.tcgplayer_price(product, tcgplayer_id)
        if tcg:
            for q, _ in tpx:
                q.tcg_product_id, q.tcg_market, q.tcg_low = tcg.product_id, tcg.market_cents, tcg.low_cents
                q.tcg_last, q.tcg_last_at, q.tcg_trend, q.tcg_trend_pct = tcg.last_sale_cents, tcg.last_sale_at, tcg.trend, tcg.trend_pct
        dn_q = dn_cm = None
        how = None
        if dn_source is not None:
            dn_q, how = self.dealernet_quote(product, dn_source, dn_name, dn_candidates)
            dn_cm = self.dealernet_cost_model(product, shipping_cents) if dn_q else None
        pq = ProductQuotes(product=product, tpx=tpx, dn=dn_q, dn_cm=dn_cm, dn_match=how)
        assessments = [
            assess(q, cm, dn_q, dn_cm, margin=margin if margin is not None else s.target_margin,
                   stale_days=s.stale_days, thin_units=s.thin_book_units, size_cap=s.size_cap)
            for q, cm in tpx
        ]
        return pq, assessments
