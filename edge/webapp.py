"""Hosted Live Sealed Product Radar (Railway). Read-only, standard library only.

Serves the board rendered by `edge dashboard`, keeps the board's picks and
refresh cadence in DATA_DIR/board.json, and re-runs the price refresh on that
cadence and when the Refresh button asks for it.

  GET  /              the board (dashboard.html), or a "first refresh" page
  GET  /api/board     {"products": [...], "hours": n}
  PUT  /api/board     same shape; saves picks and cadence
  POST /api/refresh   start a board-only refresh now (no-op while one runs)
  GET  /api/status    refresh state and when the board was last built
  GET  /healthz       liveness, no auth

Each refresh runs `edge dashboard` in a child process with DATA_DIR as its
working directory, so .cache/ (history, catalogue, Dealernet session) and
mappings.yaml live on the Railway volume. A full refresh (`--universe N`) also
prices the N hottest products per vertical; it runs at most hourly, and the
runs in between price only the board.

Environment: PORT, DATA_DIR (default ./data), APP_PASSWORD (optional HTTP basic
auth, any user name), HOT_COUNT (default 25), FULL_EVERY_S (default 3600),
EDGE_DEMO=1 (offline demo fixtures), plus everything edge.config reads
(TPX_API_KEY, DEALERNET_ACCESS, DEALERNET_USERNAME, DEALERNET_PASSWORD, ...).
"""
from __future__ import annotations

import base64
import hmac
import json
import logging
import os
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Optional

import yaml

log = logging.getLogger("edge.webapp")

PKG_ROOT = Path(__file__).resolve().parent.parent
SEED_BOARD = PKG_ROOT / "board.seed.json"
REFRESH_TIMEOUT_S = 45 * 60
# Cadence choices the page offers, in hours (0 = paused).
MIN_HOURS = 0.25


def _now() -> float:
    return time.time()


def _iso(ts: Optional[float]) -> Optional[str]:
    return datetime.fromtimestamp(ts, timezone.utc).isoformat() if ts else None


def clean_board(doc: dict) -> dict:
    """Validate a board document from the page: [{name, tpx_id?, shipping?}], hours >= 0."""
    products = []
    for p in (doc.get("products") or [])[:200]:
        if not isinstance(p, dict):
            continue
        name = str(p.get("name") or "").strip()[:200]
        if not name:
            continue
        item: dict = {"name": name}
        if p.get("tpx_id"):
            item["tpx_id"] = str(p["tpx_id"])[:80]
        if isinstance(p.get("shipping"), (int, float)):
            item["shipping"] = p["shipping"]
        products.append(item)
    try:
        hours = float(doc.get("hours", 1))
    except (TypeError, ValueError):
        hours = 1.0
    hours = 0.0 if hours <= 0 else max(MIN_HOURS, min(hours, 24 * 7))
    return {"products": products, "hours": hours}


def watchlist_items(board: dict) -> list[dict]:
    """Board products as `edge dashboard` watchlist items (packs/tins/blisters ship for $4)."""
    out = []
    for p in board["products"]:
        item = {"name": p["name"]}
        if p.get("tpx_id"):
            item["tpx_id"] = p["tpx_id"]
        if p.get("shipping") is not None:
            item["shipping"] = p["shipping"]
        elif any(w in p["name"].lower() for w in ("pack", "tin", "blister")):
            item["shipping"] = 4
        out.append(item)
    return out


class Radar:
    """Board storage plus the refresh runner and its schedule."""

    def __init__(self, data_dir: Path, *, hot_count: int = 25, full_every_s: int = 3600, demo: bool = False):
        self.data = Path(data_dir)
        self.data.mkdir(parents=True, exist_ok=True)
        self.board_path = self.data / "board.json"
        self.page_path = self.data / "dashboard.html"
        self.state_path = self.data / "refresh_state.json"
        self.hot_count, self.full_every_s, self.demo = hot_count, full_every_s, demo
        self._lock = threading.Lock()
        self._board_lock = threading.Lock()
        self.running: Optional[dict] = None
        st = self._read_json(self.state_path) or {}
        self.last_ok: Optional[float] = st.get("last_ok")
        self.last_full: Optional[float] = st.get("last_full")
        self.last_run: Optional[float] = st.get("last_run")
        self.last_error: Optional[str] = st.get("last_error")

    # -- storage -----------------------------------------------------------
    @staticmethod
    def _read_json(path: Path) -> Optional[dict]:
        try:
            return json.loads(path.read_text())
        except (OSError, ValueError):
            return None

    @staticmethod
    def _write_json(path: Path, doc: dict) -> None:
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(doc, indent=1))
        tmp.replace(path)

    def board(self) -> dict:
        doc = self._read_json(self.board_path)
        if doc is None:
            doc = self._read_json(SEED_BOARD) or {"products": [], "hours": 1}
        return clean_board(doc)

    def save_board(self, doc: dict) -> dict:
        board = clean_board(doc)
        board["updated"] = datetime.now(timezone.utc).isoformat()
        with self._board_lock:
            self._write_json(self.board_path, board)
        return board

    def _save_state(self) -> None:
        self._write_json(self.state_path, {"last_ok": self.last_ok, "last_full": self.last_full,
                                           "last_run": self.last_run, "last_error": self.last_error})

    def status(self) -> dict:
        page_at = self.page_path.stat().st_mtime if self.page_path.exists() else None
        run = self.running
        return {"running": bool(run), "kind": run["kind"] if run else None,
                "started_at": _iso(run["started"]) if run else None,
                "page_at": _iso(page_at), "last_ok": _iso(self.last_ok), "last_full": _iso(self.last_full),
                "last_error": self.last_error, "hours": self.board()["hours"]}

    # -- refresh -----------------------------------------------------------
    def start(self, kind: str) -> bool:
        """Start a refresh in the background; False if one is already running."""
        with self._lock:
            if self.running:
                return False
            self.running = {"kind": kind, "started": _now()}
        threading.Thread(target=self._run, args=(kind,), daemon=True, name=f"refresh-{kind}").start()
        return True

    def command(self, kind: str, watchlist: Path) -> list[str]:
        cmd = [sys.executable, "-c", "from edge.cli import app; app(prog_name='edge')"]
        if self.demo:
            cmd.append("--demo")
        cmd += ["dashboard", str(watchlist), "-o", str(self.page_path) + ".new",
                "--universe", str(self.hot_count if kind == "full" else 0)]
        if self.demo:
            cmd += ["--dn-csv", str(PKG_ROOT / "dealernet.example.csv")]
        return cmd

    def _run(self, kind: str) -> None:
        started = _now()
        watchlist = self.data / "watchlist.board.yaml"
        try:
            items = watchlist_items(self.board())
            if self.demo and not items:
                items = yaml.safe_load((PKG_ROOT / "watchlist.example.yaml").read_text())["products"]
            watchlist.write_text(yaml.safe_dump({"products": items}, sort_keys=False, allow_unicode=True))
            log.info("refresh (%s) started: %d board products", kind, len(items))
            res = subprocess.run(self.command(kind, watchlist), cwd=self.data, capture_output=True, text=True,
                                 timeout=REFRESH_TIMEOUT_S)
            new = Path(str(self.page_path) + ".new")
            if res.returncode != 0 or not new.exists():
                tail = (res.stderr or res.stdout or "").strip().splitlines()[-5:]
                raise RuntimeError(f"edge dashboard exited {res.returncode}: " + " | ".join(tail))
            new.replace(self.page_path)
            self.last_ok, self.last_error = _now(), None
            if kind == "full":
                self.last_full = started
            log.info("refresh (%s) done in %.0f s", kind, _now() - started)
        except Exception as exc:  # keep serving the last good board
            self.last_error = f"{datetime.now(timezone.utc):%b %d %H:%M} UTC: {exc}"[:600]
            log.error("refresh (%s) failed: %s", kind, exc)
        finally:
            self.last_run = started
            self._save_state()
            with self._lock:
                self.running = None

    def due(self) -> Optional[str]:
        """Which refresh the schedule wants now: 'full', 'board' or None."""
        if not self.page_path.exists():
            return "full"
        hours = self.board()["hours"]
        if hours <= 0:
            return None
        if self.last_run and _now() - self.last_run < hours * 3600:
            return None
        if not self.last_full or _now() - self.last_full >= self.full_every_s:
            return "full"
        return "board"

    def scheduler(self, tick_s: int = 30) -> None:
        while True:
            try:
                kind = self.due()
                if kind:
                    self.start(kind)
            except Exception:
                log.exception("scheduler tick failed")
            time.sleep(tick_s)


# ---------------------------------------------------------------------------
PENDING_PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><meta http-equiv="refresh" content="20">
<title>Live Sealed Product Radar</title><style>body{background:#0a0a0b;color:#f2f2f3;font:15px/1.5 system-ui,sans-serif;
margin:0;display:grid;place-items:center;min-height:100vh;padding:16px}main{max-width:460px}.m{color:#8c8c95}</style></head>
<body><main><h1>Live Sealed Product Radar</h1><p>The first price pull is running (Tradepost, Dealernet, TCGplayer).
It takes about 10 minutes; this page reloads itself.</p><p class="m">%s</p></main></body></html>"""

# Tells the board's script it is served by this app (saves go to /api/board, Refresh to /api/refresh).
API_FLAG = "<script>window.RADAR_API=true;</script>"


def make_handler(radar: Radar, password: Optional[str]):
    class Handler(BaseHTTPRequestHandler):
        server_version = "radar"

        def log_message(self, fmt, *args):  # noqa: N802 - stdlib signature
            log.info("%s %s", self.address_string(), fmt % args)

        def _send(self, code: int, body: bytes, ctype: str, extra: Optional[dict] = None) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def _json(self, code: int, doc: dict) -> None:
            self._send(code, json.dumps(doc).encode(), "application/json")

        def _authed(self) -> bool:
            if not password:
                return True
            hdr = self.headers.get("Authorization", "")
            if hdr.startswith("Basic "):
                try:
                    _, _, given = base64.b64decode(hdr[6:]).decode().partition(":")
                except ValueError:
                    given = ""
                if hmac.compare_digest(given.encode(), password.encode()):
                    return True
            self._send(401, b"Password required", "text/plain",
                       {"WWW-Authenticate": 'Basic realm="Live Sealed Product Radar", charset="UTF-8"'})
            return False

        def _body(self) -> dict:
            n = int(self.headers.get("Content-Length") or 0)
            if n > 256_000:
                raise ValueError("body too large")
            doc = json.loads(self.rfile.read(n) or b"{}")
            if not isinstance(doc, dict):
                raise ValueError("expected a JSON object")
            return doc

        def do_HEAD(self):  # noqa: N802
            self.do_GET()

        def do_GET(self):  # noqa: N802
            path = self.path.split("?", 1)[0]
            if path == "/healthz":
                return self._send(200, b"ok", "text/plain")
            if not self._authed():
                return
            if path == "/":
                if radar.page_path.exists():
                    page = radar.page_path.read_text(encoding="utf-8").replace("</head>", API_FLAG + "</head>", 1)
                    return self._send(200, page.encode("utf-8"), "text/html; charset=utf-8")
                err = radar.last_error or ""
                return self._send(200, (PENDING_PAGE % err.replace("<", "&lt;")).encode(), "text/html; charset=utf-8")
            if path == "/api/board":
                return self._json(200, radar.board())
            if path == "/api/status":
                return self._json(200, radar.status())
            self._send(404, b"Not found", "text/plain")

        def do_PUT(self):  # noqa: N802
            if not self._authed():
                return
            if self.path.split("?", 1)[0] != "/api/board":
                return self._send(404, b"Not found", "text/plain")
            try:
                board = radar.save_board(self._body())
            except (ValueError, json.JSONDecodeError) as exc:
                return self._json(400, {"error": str(exc)})
            self._json(200, board)

        def do_POST(self):  # noqa: N802
            path = self.path.split("?", 1)[0]
            if path == "/api/board":
                return self.do_PUT()
            if not self._authed():
                return
            if path == "/api/refresh":
                started = radar.start("board")
                return self._json(202, {"started": started, **radar.status()})
            self._send(404, b"Not found", "text/plain")

    return Handler


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    radar = Radar(Path(os.environ.get("DATA_DIR", "data")).resolve(),
                  hot_count=int(os.environ.get("HOT_COUNT", "25")),
                  full_every_s=int(os.environ.get("FULL_EVERY_S", "3600")),
                  demo=os.environ.get("EDGE_DEMO", "").lower() in ("1", "true", "yes"))
    if not radar.demo and not os.environ.get("TPX_API_KEY"):
        log.warning("TPX_API_KEY is not set: refreshes will fail until it is added")
    threading.Thread(target=radar.scheduler, daemon=True, name="scheduler").start()
    port = int(os.environ.get("PORT", "8080"))
    srv = ThreadingHTTPServer(("0.0.0.0", port), make_handler(radar, os.environ.get("APP_PASSWORD") or None))
    log.info("serving on :%d, data in %s", port, radar.data)
    srv.serve_forever()


if __name__ == "__main__":
    main()
