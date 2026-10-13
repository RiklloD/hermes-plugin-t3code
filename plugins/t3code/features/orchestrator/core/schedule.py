"""Schedule triggers (T3's interval / fixed_time / webhook) and the webhook listener."""

from __future__ import annotations

import hashlib
import hmac
import base64
import json
import logging
import re
import threading
import time
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Optional

logger = logging.getLogger("t3o.schedule")

MIN_INTERVAL_MS = 60_000
_TIME = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")


def normalize(schedule: Any) -> dict:
    """Validate a trigger object. Accepts the JSON-string compatibility form."""
    if isinstance(schedule, str):
        try:
            schedule = json.loads(schedule)
        except ValueError as exc:
            raise ValueError("schedule must be an object like {type:'interval', everyMs:3600000}") from exc
    if not isinstance(schedule, dict):
        raise ValueError("schedule must be an object")
    kind = schedule.get("type")
    if kind == "interval":
        every = schedule.get("everyMs", schedule.get("every_ms"))
        if not isinstance(every, (int, float)) or every < MIN_INTERVAL_MS:
            raise ValueError("interval schedules need everyMs >= 60000 (one minute)")
        return {"type": "interval", "everyMs": int(every)}
    if kind == "fixed_time":
        tod = str(schedule.get("timeOfDay") or schedule.get("time_of_day") or "")
        if not _TIME.match(tod):
            raise ValueError("fixed_time schedules need timeOfDay as HH:MM (24h, local time)")
        days = schedule.get("weekdays")
        if days is not None:
            if not isinstance(days, list) or not all(isinstance(d, int) and 0 <= d <= 6 for d in days):
                raise ValueError("weekdays are integers 0 (Sunday) … 6 (Saturday)")
            days = sorted(set(days)) or None
        h, m = tod.split(":")
        return {"type": "fixed_time", "timeOfDay": f"{int(h):02d}:{m}", **({"weekdays": days} if days else {})}
    if kind == "webhook":
        return {"type": "webhook"}
    raise ValueError("schedule.type must be 'interval', 'fixed_time' or 'webhook'")


def next_run(schedule: dict, *, after: float, last_run: Optional[float] = None) -> Optional[float]:
    kind = schedule.get("type")
    if kind == "interval":
        every = schedule["everyMs"] / 1000.0
        base = last_run if last_run else after
        nxt = base + every
        while nxt <= after:
            nxt += every
        return nxt
    if kind == "fixed_time":
        h, m = (int(x) for x in schedule["timeOfDay"].split(":"))
        days = schedule.get("weekdays")
        start = datetime.fromtimestamp(after)
        for offset in range(0, 8):
            day = start + timedelta(days=offset)
            cand = day.replace(hour=h, minute=m, second=0, microsecond=0)
            if cand.timestamp() <= after:
                continue
            # Python: Monday=0 … Sunday=6; T3: Sunday=0 … Saturday=6.
            if days and ((cand.weekday() + 1) % 7) not in days:
                continue
            return cand.timestamp()
        return None
    return None  # webhooks run on request


def describe(schedule: dict) -> str:
    kind = schedule.get("type")
    if kind == "interval":
        ms = schedule["everyMs"]
        for unit, size in (("day", 86_400_000), ("hour", 3_600_000), ("minute", 60_000)):
            if ms % size == 0:
                n = ms // size
                return f"every {n} {unit}{'s' if n != 1 else ''}"
        return f"every {ms // 1000}s"
    if kind == "fixed_time":
        names = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"]
        days = schedule.get("weekdays")
        when = "daily" if not days else ("weekdays" if days == [1, 2, 3, 4, 5] else ",".join(names[d] for d in days))
        return f"{when} at {schedule['timeOfDay']}"
    if kind == "webhook":
        return "on webhook request"
    return str(kind)


# ── placeholders ────────────────────────────────────────────────────────────

_PLACEHOLDER = re.compile(r"\{\{\s*([a-zA-Z_][\w.\-]*)\s*\}\}")
_SECRET_HEADERS = {"authorization", "cookie", "x-hub-signature", "x-hub-signature-256", "x-signature", "proxy-authorization"}


def render_prompt(prompt: str, request: Optional[dict], *, signed: bool = False) -> str:
    """Fill {{…}} placeholders. With a signature only the HMAC-covered body is trusted:
    header, query and whole-request placeholders render empty, so an attacker replaying a
    valid body cannot inject instructions through unsigned parts of the request."""
    if not request:
        return prompt

    def lookup(path: str) -> str:
        if signed and path.split(".", 1)[0] in ("headers", "query", "request"):
            return ""
        if path == "request":
            safe = {**request, "headers": {k: ("[redacted]" if k.lower() in _SECRET_HEADERS else v)
                                          for k, v in (request.get("headers") or {}).items()}}
            return json.dumps(safe, ensure_ascii=False)[:20000]
        head, _, rest = path.partition(".")
        if head == "body":
            value: Any = request.get("body")
            for part in rest.split(".") if rest else []:
                value = value.get(part) if isinstance(value, dict) else None
            return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)[:20000]
        if head == "headers":
            headers = {k.lower(): v for k, v in (request.get("headers") or {}).items()}
            return "" if rest.lower() in _SECRET_HEADERS else str(headers.get(rest.lower(), ""))
        if head == "query":
            return str((request.get("query") or {}).get(rest, ""))
        return "{{" + path + "}}"

    return _PLACEHOLDER.sub(lambda m: lookup(m.group(1)), prompt)


def verify_signature(signature: Optional[dict], headers: dict, raw: bytes) -> bool:
    if not signature:
        return True
    secret = str(signature.get("secret") or "")
    if not secret:
        return False
    header = {k.lower(): v for k, v in headers.items()}.get(str(signature.get("header", "")).lower(), "")
    prefix = str(signature.get("prefix") or "")
    if prefix and not header.startswith(prefix):
        return False
    given = header[len(prefix):]
    digest = hmac.new(secret.encode(), raw, hashlib.sha256).digest()
    expected = digest.hex() if signature.get("encoding", "hex") == "hex" else base64.b64encode(digest).decode()
    return hmac.compare_digest(given.strip(), expected)


class WebhookListener:
    """Loopback-only HTTP listener: POST/GET /hook/<token>. Put a tunnel in front to receive internet webhooks."""

    def __init__(self, on_request: Callable[[str, dict, bytes], tuple[int, dict]]) -> None:
        self.on_request = on_request
        self.port = 0
        self._server: Optional[ThreadingHTTPServer] = None
        self._lock = threading.Lock()

    def ensure(self, port: int) -> Optional[int]:
        with self._lock:
            if port <= 0:
                self.stop()
                return None
            if self._server is not None and self.port == port:
                return port
            self.stop()
            handler = self._handler()
            try:
                srv = ThreadingHTTPServer(("127.0.0.1", port), handler)
            except OSError as exc:
                logger.warning("t3o webhook listener could not bind 127.0.0.1:%s: %s", port, exc)
                return None
            srv.daemon_threads = True
            threading.Thread(target=srv.serve_forever, daemon=True, name="t3o-webhooks").start()
            self._server, self.port = srv, port
            return port

    def stop(self) -> None:
        if self._server is not None:
            try:
                self._server.shutdown()
                self._server.server_close()
            except Exception:
                pass
        self._server, self.port = None, 0

    def _handler(self):
        listener = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):  # noqa: D401 - silence default stderr logging
                return

            def _serve(self):
                from urllib.parse import parse_qs, urlparse
                url = urlparse(self.path)
                parts = [p for p in url.path.split("/") if p]
                if len(parts) != 2 or parts[0] != "hook":
                    return self._reply(404, {"error": "not found"})
                length = int(self.headers.get("content-length") or 0)
                raw = self.rfile.read(min(length, 1_000_000)) if length else b""
                try:
                    body: Any = json.loads(raw) if raw else None
                except ValueError:
                    body = raw.decode("utf-8", "replace")
                request = {"method": self.command, "headers": dict(self.headers.items()), "body": body,
                           "query": {k: v[0] for k, v in parse_qs(url.query).items()}, "received_at": time.time()}
                code, payload = listener.on_request(parts[1], request, raw)
                self._reply(code, payload)

            def _reply(self, code: int, payload: dict):
                data = json.dumps(payload).encode()
                self.send_response(code)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            do_POST = _serve
            do_GET = _serve
            do_PUT = _serve

        return Handler
