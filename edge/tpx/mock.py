"""Offline mock of the TPX GraphQL endpoint, served from JSON fixtures.

Used by `edge --demo ...` and by the test-suite. Fixture files are either
recordings made with `edge --record` ({operation, variables, response}) or the
hand-written demo set in tests/fixtures/demo. Routing is by operation name,
then by the most specific variables match.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Optional

import httpx

_OP_RE = re.compile(r"\b(query|mutation)\s+(\w+)")


class FixtureStore:
    def __init__(self, directory: Path):
        self.by_op: dict[str, list[tuple[dict, dict]]] = {}
        for f in sorted(Path(directory).glob("*.json")):
            doc = json.loads(f.read_text())
            op = doc.get("operation")
            if not op:
                continue
            self.by_op.setdefault(op, []).append((doc.get("variables") or {}, doc.get("response") or {}))

    def lookup(self, op: str, variables: dict) -> Optional[dict]:
        cands = self.by_op.get(op)
        if not cands:
            return None
        best, best_score = None, -1
        flat_req = _flatten(variables)
        for vars_, resp in cands:
            flat = _flatten(vars_)
            if any(flat_req.get(k) != v for k, v in flat.items() if k in flat_req):
                continue  # conflicting value -> not a match
            sc = sum(1 for k, v in flat.items() if flat_req.get(k) == v)
            if sc > best_score:
                best, best_score = resp, sc
        return best


def _flatten(d: Any, prefix: str = "") -> dict[str, Any]:
    out: dict[str, Any] = {}
    if isinstance(d, dict):
        for k, v in d.items():
            out.update(_flatten(v, f"{prefix}{k}."))
    elif isinstance(d, list):
        out[prefix[:-1]] = json.dumps(d, sort_keys=True)
    else:
        out[prefix[:-1]] = d
    return out


def mock_transport(directory: Path, *, require_key: str | None = None) -> httpx.MockTransport:
    store = FixtureStore(directory)

    def handler(request: httpx.Request) -> httpx.Response:
        if require_key is not None and request.headers.get("X-API-Key") != require_key:
            return httpx.Response(200, json={"errors": [{"message": "Unauthenticated: invalid API key",
                                                         "extensions": {"code": "UNAUTHENTICATED"}}]})
        body = json.loads(request.content or b"{}")
        q = body.get("query", "")
        m = _OP_RE.search(q)
        op = m.group(2) if m else "anonymous"
        if m and m.group(1) == "mutation":
            return httpx.Response(200, json={"errors": [{"message": "mutations are not served by the mock"}]})
        resp = store.lookup(op, body.get("variables") or {})
        if resp is None:
            return httpx.Response(200, json={"errors": [{"message": f"mock: no fixture for operation {op}"}]})
        return httpx.Response(200, json=resp)

    return httpx.MockTransport(handler)
