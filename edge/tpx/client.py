"""TPX GraphQL client.

* Auth header is exactly `X-API-Key`.
* Auth failures come back HTTP 200 with a top-level `errors` array -> TPXAuthError.
* Domain failures come back as per-payload `error { message code }` -> TPXDomainError.
* Client-side token-bucket limiter, exponential backoff on 429/5xx/transport
  errors, short TTL cache, optional response recorder for test fixtures.

The key is never logged or included in exceptions.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import threading
import time
from pathlib import Path
from typing import Any, Callable, Optional

import httpx

from . import queries as Q

log = logging.getLogger("edge.tpx")

_MUTATION_RE = re.compile(r"^\s*mutation\b", re.IGNORECASE | re.MULTILINE)


class TPXError(RuntimeError):
    pass


class TPXAuthError(TPXError):
    pass


class TPXGraphQLError(TPXError):
    def __init__(self, errors: list[dict]):
        self.errors = errors
        super().__init__("; ".join(e.get("message", str(e)) for e in errors))


class TPXDomainError(TPXError):
    def __init__(self, operation: str, error: dict):
        self.operation = operation
        self.code = error.get("code")
        super().__init__(f"{operation}: {error.get('message')} ({self.code})")


class TPXTransportError(TPXError):
    pass


class ReadOnlyViolation(TPXError):
    pass


class TokenBucket:
    def __init__(self, rate_per_s: float, burst: Optional[int] = None):
        self.rate = max(rate_per_s, 0.01)
        self.capacity = burst or max(1, int(rate_per_s))
        self.tokens = float(self.capacity)
        self.updated = time.monotonic()
        self._lock = threading.Lock()

    def acquire(self) -> None:
        with self._lock:
            now = time.monotonic()
            self.tokens = min(self.capacity, self.tokens + (now - self.updated) * self.rate)
            self.updated = now
            if self.tokens < 1:
                wait = (1 - self.tokens) / self.rate
                time.sleep(wait)
                self.tokens = 0.0
                self.updated = time.monotonic()
            else:
                self.tokens -= 1


class TTLCache:
    def __init__(self, ttl_s: float):
        self.ttl = ttl_s
        self._data: dict[str, tuple[float, Any]] = {}
        self._lock = threading.Lock()

    def get(self, key: str) -> Any:
        with self._lock:
            hit = self._data.get(key)
            if not hit:
                return None
            exp, val = hit
            if exp < time.monotonic():
                del self._data[key]
                return None
            return val

    def set(self, key: str, val: Any, ttl: Optional[float] = None) -> None:
        with self._lock:
            self._data[key] = (time.monotonic() + (self.ttl if ttl is None else ttl), val)

    def clear(self) -> None:
        with self._lock:
            self._data.clear()


def _op_name(query: str) -> str:
    m = re.search(r"\b(query|mutation|subscription)\s+(\w+)", query)
    return m.group(2) if m else "anonymous"


def _payload_field(query: str) -> Optional[str]:
    """First top-level field selected in the query, e.g. getOrderBook."""
    body = query.split("{", 1)[1] if "{" in query else ""
    m = re.search(r"\s*(\w+)\s*[\(\{]", body)
    return m.group(1) if m else None


class TPXClient:
    def __init__(
        self,
        api_key: str,
        url: str = "https://platform-api.tradepost.co/graphql",
        *,
        rate_limit_rps: float = 4.0,
        cache_ttl_s: float = 30.0,
        timeout_s: float = 20.0,
        max_retries: int = 4,
        transport: Optional[httpx.BaseTransport] = None,
        recorder: Optional[Callable[[str, dict, dict], None]] = None,
        sleep: Callable[[float], None] = time.sleep,
    ):
        if not api_key:
            raise TPXAuthError("empty API key")
        self.url = url
        self._http = httpx.Client(
            headers={"X-API-Key": api_key, "Content-Type": "application/json", "Accept": "application/json"},
            timeout=timeout_s,
            transport=transport,
        )
        self.limiter = TokenBucket(rate_limit_rps)
        self.cache = TTLCache(cache_ttl_s)
        self.max_retries = max_retries
        self.recorder = recorder
        self._sleep = sleep
        self.request_count = 0

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> "TPXClient":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # ------------------------------------------------------------------
    def execute(self, query: str, variables: Optional[dict] = None, *, cache_ttl: Optional[float] = None,
                use_cache: bool = True) -> dict:
        """Run a query and return the payload of its first top-level field.

        Raises TPXAuthError / TPXGraphQLError on top-level errors (checked
        *before* reading data) and TPXDomainError on a per-payload `error`.
        """
        if _MUTATION_RE.search(query) or any(f in query for f in ("createOrderSubmission",)):
            raise ReadOnlyViolation("This tool is read-only; mutations are not permitted.")
        variables = variables or {}
        key = hashlib.sha256(json.dumps([query, variables], sort_keys=True, default=str).encode()).hexdigest()
        if use_cache:
            cached = self.cache.get(key)
            if cached is not None:
                return cached

        body = self._post_with_retry({"query": query, "variables": variables})
        op = _op_name(query)

        errors = body.get("errors")
        if errors:
            msgs = " ".join((e.get("message") or "").lower() for e in errors)
            codes = " ".join(str((e.get("extensions") or {}).get("code", "")).lower() for e in errors)
            if any(t in msgs or t in codes for t in ("unauthenticated", "unauthorized", "api key", "forbidden", "invalid key")):
                raise TPXAuthError(f"{op}: {errors[0].get('message')}")
            raise TPXGraphQLError(errors)

        data = body.get("data") or {}
        field = _payload_field(query)
        payload = data.get(field) if field else data
        if payload is None:
            raise TPXGraphQLError([{"message": f"{op}: empty data for field {field!r}"}])
        if isinstance(payload, dict) and payload.get("error"):
            raise TPXDomainError(op, payload["error"])

        if self.recorder:
            try:
                self.recorder(op, variables, body)
            except Exception:  # pragma: no cover - never fail a call on recording
                log.exception("fixture recorder failed")
        if use_cache:
            self.cache.set(key, payload, cache_ttl)
        return payload

    def _post_with_retry(self, json_body: dict) -> dict:
        delay = 0.5
        last_exc: Optional[Exception] = None
        for attempt in range(self.max_retries + 1):
            self.limiter.acquire()
            try:
                self.request_count += 1
                resp = self._http.post(self.url, json=json_body)
            except httpx.HTTPError as exc:
                last_exc = TPXTransportError(f"transport error: {exc.__class__.__name__}")
            else:
                if resp.status_code == 429 or resp.status_code >= 500:
                    retry_after = resp.headers.get("Retry-After")
                    last_exc = TPXTransportError(f"HTTP {resp.status_code}")
                    if retry_after and retry_after.isdigit():
                        delay = max(delay, float(retry_after))
                elif resp.status_code in (401, 403):
                    raise TPXAuthError(f"HTTP {resp.status_code} from TPX (check X-API-Key)")
                elif resp.status_code >= 400:
                    raise TPXTransportError(f"HTTP {resp.status_code}: {resp.text[:200]}")
                else:
                    try:
                        return resp.json()
                    except ValueError as exc:
                        raise TPXTransportError(f"non-JSON response: {exc}") from exc
            if attempt < self.max_retries:
                log.debug("retrying after %s (%.1fs)", last_exc, delay)
                self._sleep(delay)
                delay = min(delay * 2, 16.0)
        raise last_exc or TPXTransportError("unknown transport failure")

    # ------------------------------------------------------------------
    def introspect(self) -> dict:
        body = self._post_with_retry({"query": Q.INTROSPECTION, "variables": {}})
        if body.get("errors"):
            raise TPXGraphQLError(body["errors"])
        return body["data"]["__schema"]


class FixtureRecorder:
    """Write each response as tests/fixtures/<op>__<hash>.json (secrets are never in bodies)."""

    def __init__(self, directory: Path):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)

    def __call__(self, op: str, variables: dict, body: dict) -> None:
        h = hashlib.sha1(json.dumps(variables, sort_keys=True, default=str).encode()).hexdigest()[:8]
        path = self.directory / f"{op}__{h}.json"
        path.write_text(json.dumps({"operation": op, "variables": variables, "response": body}, indent=2, default=str))
