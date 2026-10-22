"""Product pictures for the dashboard, embedded as data URIs.

The page is a single file with no external loads, so each 200x200 thumbnail
is fetched once from TPX's CDN, cached on disk, and inlined. When the CDN is
unreachable the tile shows a placeholder instead; nothing fails.
"""
from __future__ import annotations

import base64
import hashlib
import logging
from pathlib import Path
from typing import Optional

import httpx

log = logging.getLogger("edge.images")
_warned = False


def image_data_uri(url: str, cache_dir: Path, timeout_s: float = 15.0) -> Optional[str]:
    global _warned
    folder = Path(cache_dir) / "images"
    key = hashlib.sha1(url.encode()).hexdigest()[:20]
    ext = url.rsplit(".", 1)[-1].lower() if "." in url.rsplit("/", 1)[-1] else "webp"
    path = folder / f"{key}.{ext}"
    try:
        if not path.exists():
            if _warned:
                return None
            r = httpx.get(url, timeout=timeout_s)
            r.raise_for_status()
            folder.mkdir(parents=True, exist_ok=True)
            path.write_bytes(r.content)
        mime = {"webp": "image/webp", "png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg"}.get(ext, "image/webp")
        return f"data:{mime};base64,{base64.b64encode(path.read_bytes()).decode()}"
    except (httpx.HTTPError, OSError) as exc:
        if not _warned:
            log.warning("product images unavailable (%s): %s", url.split("/")[2], exc)
            _warned = True
        return None
