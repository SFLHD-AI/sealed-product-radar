"""Catalog: asset types, MINT key resolution (cached), product search."""
from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Optional

from ..models import Product
from . import queries as Q
from .client import TPXClient, TPXDomainError, TPXGraphQLError

log = logging.getLogger("edge.catalog")

VERTICAL_ALIASES = {
    "sealed_wax": ("sealed wax", "sealed_wax", "sealedwax", "wax"),
    "sealed_tcg": ("sealed tcg", "sealed_tcg", "sealedtcg", "tcg"),
}

CASE_RE = re.compile(r"\b(case|cs)\b", re.IGNORECASE)
FORMAT_RE = re.compile(
    r"\b(hobby|jumbo|hta|mega|blaster|hanger|retail|cello|value|fat\s*pack|booster|collector|elite trainer|etb|"
    r"bundle|tin|display|case)\b",
    re.IGNORECASE,
)


class IdCache:
    """Tiny JSON cache for IDs that never change (asset types, condition keys)."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self._data: dict = {}
        if self.path.exists():
            try:
                self._data = json.loads(self.path.read_text())
            except ValueError:
                self._data = {}

    def get(self, key: str):
        return self._data.get(key)

    def set(self, key: str, value) -> None:
        self._data[key] = value
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self._data, indent=2))


class Catalog:
    def __init__(self, client: TPXClient, cache_dir: Path, pinned_asset_types: Optional[dict[str, str]] = None):
        self.client = client
        self.ids = IdCache(Path(cache_dir) / "tpx_ids.json")
        self.pinned = {k: v for k, v in (pinned_asset_types or {}).items() if v}

    # --- asset types ------------------------------------------------------
    def list_asset_types(self) -> list[dict]:
        cached = self.ids.get("asset_types")
        if cached:
            return cached
        try:
            payload = self.client.execute(Q.LIST_ASSET_TYPES, {"input": {}}, cache_ttl=3600)
            types = payload.get("assetTypes") or []
        except (TPXGraphQLError, TPXDomainError) as exc:
            log.warning("listAssetTypes unavailable (%s); relying on pinned IDs", exc)
            types = []
        if types:
            self.ids.set("asset_types", types)
        return types

    def asset_type_ids(self, vertical: str) -> dict[str, str]:
        """{'sealed_wax': id, 'sealed_tcg': id} for the requested vertical(s)."""
        wanted = ["sealed_wax", "sealed_tcg"] if vertical == "both" else [vertical]
        out: dict[str, str] = {}
        for v in wanted:
            if v in self.pinned:
                out[v] = self.pinned[v]
        missing = [v for v in wanted if v not in out]
        if missing:
            for t in self.list_asset_types():
                label = f"{t.get('type', '')} {t.get('displayName', '')} {t.get('slug', '')}".lower()
                for v in missing:
                    if any(a in label for a in VERTICAL_ALIASES[v]):
                        out[v] = str(t["id"])
        still = [v for v in wanted if v not in out]
        if still:
            raise RuntimeError(
                f"Could not resolve TPX asset type id for {still}. Pin TPX_ASSET_TYPE_SEALED_WAX / "
                "TPX_ASSET_TYPE_SEALED_TCG in .env (IDs are in the TPX docs under Verticals, or run `edge doctor`)."
            )
        return out

    # --- condition key --------------------------------------------------
    def resolve_condition_key(self, asset_type_id: str, value: str = "MINT") -> str:
        ck = f"key:{asset_type_id}:{value}"
        hit = self.ids.get(ck)
        if hit:
            return hit
        payload = self.client.execute(Q.LIST_KEY_CONFIGURATIONS, {"input": {"assetTypeId": asset_type_id}}, cache_ttl=3600)
        for cfg in payload.get("keyConfigurations") or []:
            for k in (cfg.get("keyData") or {}).get("keys") or []:
                if str(k.get("keyType", "")).upper() == "SEALED_CONDITION" and str(k.get("value", "")).upper() == value.upper():
                    self.ids.set(ck, str(k["keyId"]))
                    return str(k["keyId"])
        raise RuntimeError(f"No SEALED_CONDITION={value} key for asset type {asset_type_id}")

    # --- search -----------------------------------------------------------
    def search(self, text: str, vertical: str, limit: int = 10) -> list[Product]:
        results: list[Product] = []
        for vname, at_id in self.asset_type_ids(vertical).items():
            payload = self.client.execute(
                Q.FILTER_BUILDER_SEARCH,
                {"input": {"assetTypeId": at_id, "query": text, "skipFacets": True, "previewLimit": limit}},
                cache_ttl=120,
            )
            for p in payload.get("preview") or []:
                results.append(self._to_product(p, at_id, vname))
        return results

    @staticmethod
    def _to_product(p: dict, asset_type_id: str, asset_type_name: str) -> Product:
        # `data` is a free-form JSON blob per asset type (format, format_type,
        # product_type, release_date, ...); asset-level fields are not in it.
        attrs = {str(k).lower(): v for k, v in (p.get("data") or {}).items() if not isinstance(v, (dict, list))}
        name = p.get("name") or ""
        fmt = attrs.get("format") or _guess_format(name)
        if str(attrs.get("format_type", "")).lower() == "case" and not CASE_RE.search(fmt or ""):
            fmt = f"{fmt} case" if fmt else "case"
        year = attrs.get("year") or attrs.get("release_year") or attrs.get("release_date")
        try:
            year = int(str(year)[:4]) if year else _guess_year(name)
        except ValueError:
            year = _guess_year(name)
        return Product(
            asset_configuration_id=str(p.get("id")),
            name=name,
            asset_type_id=asset_type_id,
            asset_type_name=asset_type_name,
            format=fmt,
            is_case=bool(fmt and "case" in fmt.lower()) or bool(CASE_RE.search(name)),
            year=year,
            manufacturer=attrs.get("manufacturer"),
            brand=attrs.get("brand"),
            sport=attrs.get("sport") or attrs.get("game"),
            raw=p,
        )


def _guess_format(name: str) -> Optional[str]:
    m = FORMAT_RE.search(name or "")
    return m.group(1).lower() if m else None


def _guess_year(name: str) -> Optional[int]:
    m = re.search(r"\b(19|20)\d{2}\b", name or "")
    return int(m.group(0)) if m else None
