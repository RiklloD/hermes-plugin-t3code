"""Orchestrator V2 for the stdio native runtimes (Muse Code, Antigravity).

* **Tools:** T3 Code gives every native agent its orchestration tools through a session MCP server
  (Muse: ``config.mcpServers``, ACP: ``mcpServers``). Here that server is a loopback Streamable-HTTP
  MCP endpoint in the backend process exposing the Orchestrator's ``t3_*`` tools — the same six
  tools Claude CLI gets in-process (``features/claude_cli/t3_bridge.py``). Each runtime gets its own
  bearer token, resolved per call to the Hermes chat that owns the runtime's current turn.
* **Subagents:** native children are reported to the Orchestrator's chat frame, so they appear in
  Hermes's Subagents frame with the provider's own Stop / Steer controls.

Both are no-ops when the Orchestrator feature is not loaded in this process.
"""

from __future__ import annotations

import json
import logging
import secrets
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable, Optional

logger = logging.getLogger("t3code.orchestration")
PROTOCOL_VERSION = "2025-06-18"

_server: Optional[ThreadingHTTPServer] = None
_tokens: dict[str, Callable[[], str]] = {}
_lock = threading.Lock()


def available() -> bool:
    return "t3o.tools" in sys.modules


def current_session_key() -> str:
    """The Hermes chat of the running turn (call on the turn's own thread)."""
    try:
        from gateway.session_context import get_session_env
        return str(get_session_env("HERMES_SESSION_ID") or "")
    except Exception:
        return ""


# ── subagents ────────────────────────────────────────────────────────────────────────────────────

def report_subagent(session_key: str, sub: dict, **controls) -> None:
    chatframe = sys.modules.get("t3o.chatframe")
    if chatframe is None or not session_key:
        return
    try:
        chatframe.native_update(session_key, sub, **controls)
    except Exception:
        logger.debug("native subagent report failed", exc_info=True)


# ── MCP endpoint ─────────────────────────────────────────────────────────────────────────────────

def _tools():
    return sys.modules.get("t3o.tools")


def _dispatch(message: dict, session_key: str) -> Optional[dict]:
    method, request_id = message.get("method"), message.get("id")
    if request_id is None:
        return None  # notifications (initialized, cancelled) need no answer

    def ok(result):
        return {"jsonrpc": "2.0", "id": request_id, "result": result}

    tools = _tools()
    if method == "initialize":
        version = (message.get("params") or {}).get("protocolVersion") or PROTOCOL_VERSION
        return ok({"protocolVersion": version, "capabilities": {"tools": {"listChanged": False}},
                   "serverInfo": {"name": "t3", "title": "T3 Code orchestration", "version": "1"},
                   "instructions": "Delegate work to other agents and providers with t3_delegate; "
                                   "t3_capabilities lists providers, models and profiles."})
    if method == "ping":
        return ok({})
    if method == "tools/list":
        listed = [] if tools is None else [
            {"name": name, "description": schema["description"], "inputSchema": schema["parameters"]}
            for name, schema in tools.SCHEMAS.items()]
        return ok({"tools": listed})
    if method == "tools/call":
        params = message.get("params") or {}
        name = params.get("name")
        if tools is None or name not in tools.HANDLERS:
            return ok({"content": [{"type": "text", "text": f"Unknown tool: {name}"}], "isError": True})
        try:
            text = tools.HANDLERS[name](dict(params.get("arguments") or {}), session_id=session_key)
            return ok({"content": [{"type": "text", "text": str(text)}], "isError": False})
        except Exception as exc:
            return ok({"content": [{"type": "text", "text": f"{name} failed: {exc}"}], "isError": True})
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": -32601, "message": f"Method not found: {method}"}}


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *_args):
        pass

    def _reply(self, status: int, body: Optional[dict | list] = None):
        data = b"" if body is None else json.dumps(body).encode("utf-8")
        self.send_response(status)
        if body is not None:
            self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _session(self) -> Optional[str]:
        auth = self.headers.get("Authorization") or ""
        resolve = _tokens.get(auth[7:]) if auth.startswith("Bearer ") else None
        return resolve() if resolve else None

    def do_POST(self):
        session_key = self._session()
        if session_key is None:
            return self._reply(401, {"error": "unauthorized"})
        try:
            payload = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"null")
        except ValueError:
            return self._reply(400, {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}})
        messages = payload if isinstance(payload, list) else [payload]
        answers = [a for a in (_dispatch(m, session_key) for m in messages if isinstance(m, dict)) if a]
        if not answers:
            return self._reply(202)
        self._reply(200, answers if isinstance(payload, list) else answers[0])

    def do_GET(self):  # no server-initiated stream
        self._reply(405)

    def do_DELETE(self):
        self._reply(200 if self._session() is not None else 401)


def mcp_endpoint(resolve_session: Callable[[], str]) -> Optional[tuple[str, str]]:
    """``(url, bearer token)`` for one runtime, or ``None`` when the Orchestrator is not loaded."""
    global _server
    if not available():
        return None
    with _lock:
        if _server is None:
            _server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
            _server.daemon_threads = True
            threading.Thread(target=_server.serve_forever, name="t3code-mcp", daemon=True).start()
        token = secrets.token_urlsafe(24)
        _tokens[token] = resolve_session
        return f"http://127.0.0.1:{_server.server_address[1]}/mcp", token


def revoke(token: Optional[str]) -> None:
    if token:
        _tokens.pop(token, None)
