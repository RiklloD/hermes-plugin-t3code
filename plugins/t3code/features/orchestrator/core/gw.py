"""In-process adapter over the Desktop backend (``tui_gateway`` inside ``hermes serve``).

Every orchestration action is an ordinary gateway RPC (``session.create``, ``prompt.submit``,
``session.steer``, ``session.interrupt`` …) dispatched in-process, so child agents are real
Hermes sessions: they persist in ``state.db``, show up in the sidebar and can be opened,
steered and stopped like any chat. Observation is a passive tap on ``server.write_json``,
the single funnel every session event passes through.
"""

from __future__ import annotations

import contextlib
import logging
import sys
import threading
import time
import uuid
from typing import Any, Callable, Optional

from . import authority as auth

logger = logging.getLogger("t3o.gw")

TAP_MARK = "_t3o_tap_installed"
WRAP_MARK = "_t3o_wrapped"


class GatewayError(RuntimeError):
    def __init__(self, code: Any, message: str, data: Any = None) -> None:
        super().__init__(message)
        self.code, self.data = code, data


class NullTransport:
    """Sink for sessions the orchestrator owns: their frames are observed by the tap, never printed."""

    auth_identity = None

    def write(self, obj: dict) -> bool:  # noqa: ARG002
        return True

    def close(self) -> None:
        return None


NULL = NullTransport()


def server():
    """The live ``tui_gateway.server`` module, or None outside a Desktop/TUI backend."""
    return sys.modules.get("tui_gateway.server")


def available() -> bool:
    if auth.active():
        return True
    srv = server()
    return srv is not None and hasattr(srv, "handle_request")


def is_serve_process() -> bool:
    """True inside the process that owns Desktop sessions: the gateway's session authority
    (current Hermes), or ``hermes serve`` / the dashboard (WS gateway, not the stdio TUI)."""
    if auth.active():
        return True
    srv = server()
    return srv is not None and not getattr(srv, "_stdio_is_rpc_channel", False)


def call(method: str, params: Optional[dict] = None, *, timeout: float = 120.0) -> dict:
    """Run one gateway RPC in-process with a null transport bound; return ``result`` or raise."""
    if auth.active():
        params = dict(params or {})
        home = profile_home(params["profile"]) if params.get("profile") else None
        try:
            return auth.call(method, params, home=home, timeout=timeout)
        except auth.AuthorityError as exc:
            raise GatewayError(exc.code, str(exc), exc.data) from exc
    srv = server()
    if srv is None:
        raise GatewayError("unavailable", "The Hermes Desktop backend is not running in this process.")
    from tui_gateway.transport import bind_transport, reset_transport

    rid = f"t3o-{uuid.uuid4().hex[:8]}"
    token = bind_transport(NULL)
    try:
        if method in getattr(srv, "_LONG_HANDLERS", ()):
            # Long handlers normally run on the pool; calling the admitted handler directly is the same code.
            resp = srv._handle_admitted_request({"jsonrpc": "2.0", "id": rid, "method": method, "params": params or {}})
        else:
            resp = srv.handle_request({"jsonrpc": "2.0", "id": rid, "method": method, "params": params or {}})
    finally:
        reset_transport(token)
    if not isinstance(resp, dict):
        raise GatewayError("no_response", f"{method}: no response")
    if "error" in resp:
        err = resp["error"] or {}
        raise GatewayError(err.get("code"), str(err.get("message") or "gateway error"), err.get("data"))
    return resp.get("result") or {}


# ── session lookup ──────────────────────────────────────────────────────────

def sessions_snapshot() -> list[tuple[str, dict]]:
    if auth.active():
        return auth.snapshot()
    srv = server()
    if srv is None:
        return []
    with srv._sessions_lock:
        return [(sid, s) for sid, s in srv._sessions.items() if isinstance(s, dict)]


def _agent_key(session: dict) -> str:
    agent = session.get("agent")
    return str(getattr(agent, "session_id", "") or "") if agent is not None else ""


def live_for_key(key: str) -> tuple[Optional[str], Optional[dict]]:
    """``(runtime sid, session record)`` of the live session whose stored key is ``key``."""
    if not key:
        return None, None
    if auth.active():
        return auth.live_for_key(key)
    for sid, s in sessions_snapshot():
        if s.get("_finalized") or s.get("_closing"):
            continue
        if s.get("session_key") == key or _agent_key(s) == key:
            return sid, s
    return None, None


def key_for_sid(sid: str) -> str:
    if auth.active():
        a, key = auth.locate(sid)
        return key if a is not None else ""
    srv = server()
    if srv is None or not sid:
        return ""
    s = srv._sessions.get(sid)
    if not isinstance(s, dict):
        return ""
    return _agent_key(s) or str(s.get("session_key") or "")


def session_for_agent(agent: Any) -> tuple[Optional[str], Optional[dict]]:
    if agent is None:
        return None, None
    if auth.active():
        return live_for_key(str(getattr(agent, "session_id", "") or ""))
    for sid, s in sessions_snapshot():
        if s.get("agent") is agent:
            return sid, s
    key = str(getattr(agent, "session_id", "") or "")
    return live_for_key(key)


def profile_of(session: Optional[dict]) -> Optional[str]:
    if not session:
        return None
    home = session.get("profile_home")
    if not home:
        # The backend's launch profile: name it, so it is never confused with another profile.
        srv = server()
        home = getattr(srv, "_hermes_home", None) if srv is not None else None
        if not home:
            return None
    with contextlib.suppress(Exception):
        from hermes_constants import profile_name_for_home
        return profile_name_for_home(home)
    return None


def is_running(session: Optional[dict]) -> bool:
    return bool(session and session.get("running"))


def stop_latched(session: Optional[dict]) -> bool:
    """After the user's Stop, core holds automatic turns until the next real prompt."""
    return bool(session and session.get("_turn_cancel_requested"))


def awaiting_user(sid: str, session: Optional[dict]) -> bool:
    """The turn is parked on a person: an approval, a question or a secret request is open."""
    srv = server()
    if srv is None or not session:
        return False
    with contextlib.suppress(Exception):
        if srv._open_requests(sid):
            return True
    with contextlib.suppress(Exception):
        if srv._pending_approval_request_payload(str(session.get("session_key") or "")):
            return True
    return False


def wrap_global(name: str, before: Callable[[], None]) -> bool:
    """Run ``before`` ahead of one server-level function. Split gateway modules are rebound onto
    ``server``'s globals, so every caller (signal handler, atexit) resolves the name there at call time."""
    srv = server()
    original = getattr(srv, name, None) if srv is not None else None
    if not callable(original):
        return False
    hooks = getattr(original, WRAP_MARK, None)
    if isinstance(hooks, dict):
        hooks["before"] = before
        return True
    hooks = {"before": before}

    def wrapper(*args, **kwargs):
        with contextlib.suppress(Exception):
            hooks["before"]()
        return original(*args, **kwargs)

    setattr(wrapper, WRAP_MARK, hooks)
    wrapper.__wrapped__ = original  # type: ignore[attr-defined]
    setattr(srv, name, wrapper)
    return True


def ensure_live(key: str, profile: Optional[str] = None, *, build: bool = True) -> tuple[str, dict]:
    """Return the live session for ``key``, resuming it (no client attached) when it is not open."""
    sid, s = live_for_key(key)
    if sid is not None:
        return sid, s
    if auth.active():
        home = profile_home(profile) if profile else None
        try:
            auth.call("session.resume", {"session_id": key}, home=home)
        except auth.AuthorityError as exc:
            raise GatewayError(exc.code, str(exc), exc.data) from exc
        sid, s = live_for_key(key)
        if sid is None:
            raise GatewayError("resume_failed", f"could not open session {key}")
        return sid, s
    params: dict = {"session_id": key, "omit_messages": True, "eager_build": bool(build)}
    if profile:
        params["profile"] = profile
    res = call("session.resume", params)
    sid = str(res.get("session_id") or "")
    srv = server()
    s = srv._sessions.get(sid) if srv is not None else None
    if not sid or not isinstance(s, dict):
        raise GatewayError("resume_failed", f"could not open session {key}")
    return sid, s


def wait_ready(session: dict, timeout: float = 120.0) -> bool:
    if session.get("_authority") is not None:
        return True  # the authority builds the agent when it claims the turn
    ev = session.get("agent_ready")
    if ev is None:
        return session.get("agent") is not None
    return bool(ev.wait(timeout))


def deliver_turn(sid: str, session: dict, text: str, *, display_kind: Optional[str] = None,
                 display_metadata: Optional[dict] = None, respect_stop: bool = True,
                 guard: Optional[Callable[[], bool]] = None, lock: Any = None) -> bool:
    """Start an automatic turn on an idle session. True only when the host ADMITTED the turn.

    Claim (``_notif_claim_turn``), the ``guard`` / Stop-latch re-check and the host admission
    (``_run_prompt_submit``, which returns after admission and runs the turn on its own thread)
    happen under ``lock`` — the lock the Stop observer takes — so a Stop can never land between
    the checks and the submission. Any refusal releases the claim.

    On the session authority the turn is a durable FIFO admission (``prompt.submit``) authored by
    the orchestrator; the session must be idle with nothing queued when it is admitted."""
    if session.get("_authority") is not None:
        with (lock if lock is not None else contextlib.nullcontext()):
            if guard is not None and not guard():
                return False
            fresh = auth.record(session["_authority"], sid)
            if fresh is None or fresh["running"] or fresh["queued_prompt"]:
                return False
            author = dict(auth.AUTHOR)
            label = (display_metadata or {}).get("display_text")
            if isinstance(label, str) and label:
                author["name"] = f"T3 · {label}"[:120]
            try:
                call("prompt.submit", {"session_id": sid, "text": text, "turn_author": author})
                return True
            except Exception:
                logger.warning("t3o: automatic turn failed for %s", sid, exc_info=True)
                return False
    srv = server()
    if srv is None:
        return False
    if not wait_ready(session, 120.0):
        return False
    kwargs: dict = {}
    if display_kind:
        kwargs["display_kind"] = display_kind
    if display_metadata:
        kwargs["display_metadata"] = display_metadata
    rid = f"__t3o__{int(time.time() * 1000)}"
    with (lock if lock is not None else contextlib.nullcontext()):
        if respect_stop and stop_latched(session):
            return False
        if guard is not None and not guard():
            return False
        if not srv._notif_claim_turn(session):
            return False
        try:
            accepted = bool(srv._run_prompt_submit(rid, sid, session, text, **kwargs))
        except Exception:
            logger.warning("t3o: automatic turn failed for %s", sid, exc_info=True)
            accepted = False
        if not accepted:
            with contextlib.suppress(Exception):
                srv._notif_release_turn(session)
        return accepted


def submit(sid: str, text: str) -> dict:
    """An ordinary user turn (``prompt.submit``): idle starts, busy follows the profile's busy-input mode."""
    return call("prompt.submit", {"session_id": sid, "text": text})


def steer(sid: str, text: str) -> str:
    return str(call("session.steer", {"session_id": sid, "text": text}).get("status") or "")


def redirect(sid: str, text: str) -> str:
    return str(call("session.redirect", {"session_id": sid, "text": text}).get("status") or "")


def interrupt(sid: str) -> bool:
    return bool(call("session.interrupt", {"session_id": sid}).get("interrupted", True))


# ── observation ─────────────────────────────────────────────────────────────

_HOOKS: dict = {"tap": None, "methods": {}, "installed": False}


def poll_install() -> None:
    """Install the session-authority hooks once the gateway has built its authority (the plugin
    loads before the runner exists). Cheap after the first success."""
    if _HOOKS["installed"] or not auth.active():
        return
    ok = True
    if _HOOKS["tap"] is not None:
        ok = auth.install_tap(_HOOKS["tap"]) and ok
    for name, (before, after) in _HOOKS["methods"].items():
        ok = auth.wrap_method(name, before, after) and ok
    _HOOKS["installed"] = ok
    if ok:
        logger.info("t3o: attached to the gateway session authority")


def install_tap(sink: Callable[[str, str, dict], None]) -> bool:
    """Observe every session event frame. Idempotent; never alters or delays delivery."""
    _HOOKS["tap"] = sink
    _HOOKS["installed"] = False
    poll_install()
    srv = server()
    if srv is None:
        return False
    if getattr(srv, TAP_MARK, None) is not None:
        setattr(srv, TAP_MARK, sink)
        return True
    original = srv.write_json

    def write_json(obj: dict) -> bool:
        ok = original(obj)
        try:
            if isinstance(obj, dict) and obj.get("method") == "event":
                params = obj.get("params") or {}
                fn = getattr(srv, TAP_MARK, None)
                if fn is not None:
                    fn(str(params.get("type") or ""), str(params.get("session_id") or ""), params.get("payload") or {})
        except Exception:
            logger.debug("t3o tap failed", exc_info=True)
        return ok

    write_json.__wrapped__ = original  # type: ignore[attr-defined]
    srv.write_json = write_json
    setattr(srv, TAP_MARK, sink)
    return True


def wrap_method(name: str, before: Optional[Callable[[dict], None]] = None,
                after: Optional[Callable[[dict, Any], None]] = None) -> bool:
    """Observe one RPC method (user Stop, user prompts). The original handler always runs."""
    if name in ("session.interrupt", "prompt.submit"):
        _HOOKS["methods"][name] = (before, after)
        _HOOKS["installed"] = False
        poll_install()
    srv = server()
    if srv is None:
        return False
    original = srv._methods.get(name)
    if original is None:
        return False
    hooks = getattr(original, WRAP_MARK, None)
    if hooks is not None:
        hooks["before"], hooks["after"] = before, after
        return True
    hooks = {"before": before, "after": after}

    def handler(rid, params):
        with contextlib.suppress(Exception):
            if hooks["before"] is not None:
                hooks["before"](params if isinstance(params, dict) else {})
        resp = original(rid, params)
        with contextlib.suppress(Exception):
            if hooks["after"] is not None:
                hooks["after"](params if isinstance(params, dict) else {}, resp)
        return resp

    setattr(handler, WRAP_MARK, hooks)
    handler.__wrapped__ = original  # type: ignore[attr-defined]
    srv._methods[name] = handler
    return True


# ── transcripts ─────────────────────────────────────────────────────────────

def profile_home(profile: Optional[str]):
    from pathlib import Path
    srv = server()
    if srv is not None and profile:
        with contextlib.suppress(Exception):
            home = srv._profile_home(profile)
            if home is not None:
                return Path(home)
    with contextlib.suppress(Exception):
        from hermes_constants import get_default_hermes_root
        root = Path(get_default_hermes_root())
        if profile and profile != "default":
            return root / "profiles" / profile
        return root
    return Path.home() / ".hermes"


def open_db(profile: Optional[str], *, write: bool = False):
    path = profile_home(profile) / "state.db"
    if write:
        from hermes_state_registry import acquire
        return acquire(path)
    from hermes_state import SessionDB
    return SessionDB(db_path=path, read_only=True)


def history(key: str, profile: Optional[str]) -> list[dict]:
    """Display transcript (ancestors included, row ids attached) for a stored session."""
    db = open_db(profile)
    try:
        return db.get_messages_as_conversation(key, include_ancestors=True, include_row_ids=True, include_compacted=True)
    finally:
        with contextlib.suppress(Exception):
            db.close()


def session_row(key: str, profile: Optional[str]) -> Optional[dict]:
    db = open_db(profile)
    try:
        return db.get_session(key)
    finally:
        with contextlib.suppress(Exception):
            db.close()


def intercept_method(name: str, fn: Callable[[Any, dict], Optional[dict]]) -> bool:
    """Let the orchestrator answer one RPC for its own ids: ``fn(rid, params)`` returns a full
    response dict, or None to fall through to the original handler unchanged."""
    srv = server()
    if srv is None:
        return False
    original = srv._methods.get(name)
    if original is None:
        return False
    holder = getattr(original, WRAP_MARK + "_intercept", None)
    if holder is not None:
        holder["fn"] = fn
        return True
    holder = {"fn": fn}

    def handler(rid, params):
        try:
            resp = holder["fn"](rid, params if isinstance(params, dict) else {}) if holder["fn"] else None
        except Exception:
            logger.debug("t3o intercept %s failed", name, exc_info=True)
            resp = None
        return resp if resp is not None else original(rid, params)

    setattr(handler, WRAP_MARK + "_intercept", holder)
    handler.__wrapped__ = original  # type: ignore[attr-defined]
    srv._methods[name] = handler
    return True


def ok(rid: Any, result: dict) -> dict:
    return server()._ok(rid, result)


def emit(event: str, sid: str, payload: dict) -> None:
    """Send one session event to whatever clients view ``sid`` (best effort)."""
    if auth.active():
        auth.emit(event, sid, payload)
        return
    srv = server()
    if srv is None or not sid:
        return
    with contextlib.suppress(Exception):
        srv._emit(event, sid, payload)
