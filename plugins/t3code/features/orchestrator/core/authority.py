"""Session-authority backend for ``gw`` (Hermes core with gateway-owned sessions, Oct 2026 on).

Hermes moved Desktop chats out of the ``tui_gateway`` sidecar into the messaging gateway's
``SessionAuthority`` (``gateway/session_authority.py``): one authority per served profile, each with
its own ``state.db``, durable FIFO admission ledger and event streams. Desktop chats are now
``local-…`` sessions in ``authority.sessions``; ``tui_gateway.server._sessions`` stays empty. Every
legacy lookup in ``gw`` therefore missed, and each ``t3_*`` call answered ``parent_not_active``.

This module speaks the authority's own contract, in-process:

* RPCs go through an operator ``AuthorityConnection`` (``gateway/session_controls.py``), the same
  dispatcher the Desktop's WebSocket uses, run on the gateway loop. One connection per
  (authority, subject); the subject is the Desktop user's, so children are ordinary sidebar chats
  the user can open, steer and stop.
* Events are observed by wrapping ``SessionEvents.publish``, the single funnel every session frame
  passes through (the frame shape is the legacy one: ``type``, ``session_id``, ``payload``).
* User Stop and user prompts are observed by wrapping ``AuthorityConnection.interrupt`` / ``submit``
  (our own connections are skipped, so automatic turns never clear a Stop hold).
* Cross-profile work routes to the sibling authority that serves the target profile
  (``runner.session_authorities``): a multiplexing gateway owns every profile.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import sys
import threading
import uuid
from types import SimpleNamespace
from typing import Any, Callable, Optional

logger = logging.getLogger("t3o.authority")

MARK = "_t3o_authority_hook"
CAPABILITIES = ["session:create", "session:read", "session:submit", "session:control", "session:approve",
                "session:respond"]
# Turn author shown on result cards and handoffs the orchestrator posts into a thread.
AUTHOR = {"id": "bot:t3-orchestrator", "name": "T3 orchestrator", "is_bot": True}


class AuthorityError(RuntimeError):
    def __init__(self, code: Any, message: str, data: Any = None) -> None:
        super().__init__(message)
        self.code, self.data = code, data


# ── runner, loop, authorities ───────────────────────────────────────────────

def runner() -> Any:
    mod = sys.modules.get("gateway.run_api")
    r = getattr(mod, "_ACTIVE_RUNNER", None) if mod is not None else None
    if r is None or getattr(r, "session_authority", None) is None:
        return None
    return r


def active() -> bool:
    return runner() is not None


def authorities() -> list:
    r = runner()
    if r is None:
        return []
    from gateway.session_authorities import all_authorities
    return [a for a in all_authorities(r) if a is not None and not getattr(a, "retired", False)]


def for_home(home: Any) -> Any:
    r = runner()
    if r is None or home is None:
        return None
    from gateway.session_authorities import authority_for_home
    return authority_for_home(r, home)


def launch() -> Any:
    r = runner()
    return getattr(r, "session_authority", None) if r is not None else None


_loop: Optional[asyncio.AbstractEventLoop] = None


def note_loop() -> None:
    """Remember the gateway loop when code runs on it (the publish tap calls this)."""
    global _loop
    with contextlib.suppress(RuntimeError):
        lp = asyncio.get_running_loop()
        if _loop is None or _loop.is_closed():
            _loop = lp


def loop() -> asyncio.AbstractEventLoop:
    global _loop
    if _loop is not None and not _loop.is_closed():
        return _loop
    r = runner()
    for attr in ("_loop_heartbeat_task", "_hosted_room_watcher_task"):
        task = getattr(r, attr, None)
        with contextlib.suppress(Exception):
            if task is not None:
                _loop = task.get_loop()
                return _loop
    for a in authorities():
        for live in list(a.sessions.values()):
            task = getattr(live, "task", None)
            with contextlib.suppress(Exception):
                if task is not None:
                    _loop = task.get_loop()
                    return _loop
    raise AuthorityError("unavailable", "The Hermes gateway loop is not reachable from this thread.")


def run(coro, timeout: float = 120.0) -> Any:
    """Run ``coro`` on the gateway loop from a worker thread and wait for it."""
    lp = loop()
    try:
        running = asyncio.get_running_loop()
    except RuntimeError:
        running = None
    if running is lp:
        coro.close()
        raise AuthorityError("wrong_thread", "orchestrator RPCs cannot block the gateway loop")
    return asyncio.run_coroutine_threadsafe(coro, lp).result(timeout)


# ── session lookup ──────────────────────────────────────────────────────────

def _ref(authority: Any, sid: str) -> Any:
    from gateway.session_contract import SessionRef
    return SessionRef(authority.profile_id, sid)


def locate(sid: str) -> tuple[Any, str]:
    """``(authority, logical session id)`` holding ``sid`` live, else the one whose store has it."""
    if not sid:
        return None, ""
    owners = authorities()
    for a in owners:
        if sid in a.sessions:
            return a, sid
    for a in owners:
        with contextlib.suppress(Exception):
            owner = a.logical_owner(sid)
            if owner in a.sessions:
                return a, owner
    for a in owners:
        with contextlib.suppress(Exception):
            if a.db.get_session(sid) is not None:
                return a, a.logical_owner(sid) or sid
    return None, ""


def _cwd(authority: Any, sid: str) -> Optional[str]:
    with contextlib.suppress(Exception):
        from hermes_state_local import local_receipt
        policy = (local_receipt(authority.db, sid) or {}).get("policy") or {}
        cwd = policy.get("cwd")
        return str(cwd) if cwd else None
    return None


def record(authority: Any, sid: str) -> Optional[dict]:
    """A session record in the shape the engine reads (``agent``, ``session_key``, ``running`` …)."""
    live = authority.sessions.get(sid)
    if live is None or getattr(live, "superseded", False):
        return None
    agent = None
    with contextlib.suppress(Exception):
        agent = authority.agent(_ref(authority, sid))
    statuses: set = set()
    with contextlib.suppress(Exception):
        from hermes_state_runtime import list_session_admissions
        statuses = {r.get("status") for r in list_session_admissions(authority.db, session_id=sid)}
    if agent is None:
        # No resident agent yet (fresh or evicted chat): the frozen launch policy names the route.
        with contextlib.suppress(Exception):
            from hermes_state_local import local_receipt
            policy = (local_receipt(authority.db, sid) or {}).get("policy") or {}
            agent = SimpleNamespace(provider=policy.get("provider"), model=policy.get("model"), session_id=sid)
    source = getattr(live, "source", None)
    return {"agent": agent, "session_key": sid, "profile_home": authority.profile_id,
            "running": bool(statuses & {"started", "unknown"}), "queued_prompt": "queued" in statuses,
            "user_id": getattr(source, "user_id", None), "cwd": _cwd(authority, sid),
            "_authority": authority}


def live_for_key(key: str) -> tuple[Optional[str], Optional[dict]]:
    a, sid = locate(key)
    if a is None or sid not in a.sessions:
        return None, None
    rec = record(a, sid)
    return (sid, rec) if rec is not None else (None, None)


def snapshot() -> list[tuple[str, dict]]:
    out = []
    for a in authorities():
        for sid in list(a.sessions):
            rec = record(a, sid)
            if rec is not None:
                out.append((sid, rec))
    return out


def handle(authority: Any, sid: str) -> Any:
    return authority._handle(_ref(authority, sid))


# ── RPC through an operator connection ──────────────────────────────────────

class _Sink:
    """Transport for the orchestrator's own connections: frames are observed by the tap instead."""

    auth_identity = None

    def write(self, obj: dict) -> bool:  # noqa: ARG002
        return True

    async def write_async(self, obj: dict) -> bool:  # noqa: ARG002
        return True

    def close(self) -> None:
        return None


_conns: dict[tuple, Any] = {}
_conns_lock = threading.Lock()


def default_subject() -> str:
    """The Desktop user's subject: a live local chat's, else the OS uid (``uid:501``)."""
    from gateway.config import Platform
    for a in authorities():
        for live in list(a.sessions.values()):
            source = getattr(live, "source", None)
            if source is not None and source.platform == Platform.LOCAL and getattr(source, "user_id", None):
                return str(source.user_id)
    return f"uid:{os.getuid()}"


async def _connection(authority: Any, subject: str) -> Any:
    key = (id(authority), subject)
    with _conns_lock:
        conn = _conns.get(key)
    if conn is not None and conn.authority is authority:
        return conn
    from gateway.session_controls import AuthorityConnection
    identity = {"user_id": subject, "provider": "local", "profile_id": authority.profile_id,
                "instance_id": authority.instance_id, "capabilities": list(CAPABILITIES),
                "native_bootstrap": True, "profile_scope": "host"}
    conn = AuthorityConnection(authority, _Sink(), identity, operator=True)
    conn._t3o = True  # noqa: SLF001 — the observers skip the orchestrator's own calls
    with _conns_lock:
        _conns[key] = conn
    return conn


async def _dispatch(conn: Any, method: str, params: dict) -> dict:
    resp = await conn.dispatch({"jsonrpc": "2.0", "id": f"t3o-{uuid.uuid4().hex[:8]}", "method": method,
                                "params": params})
    if not isinstance(resp, dict):
        raise AuthorityError("no_response", f"{method}: no response")
    if "error" in resp:
        err = resp["error"] or {}
        data = err.get("data") or {}
        detail = " ".join(str(data[k]) for k in ("field", "detail") if isinstance(data, dict) and data.get(k))
        raise AuthorityError(err.get("code"), f"{method}: {err.get('message') or 'refused'}"
                             + (f" ({detail})" if detail else ""), data)
    return resp.get("result") or {}


async def _subscribed(conn: Any, sid: str) -> None:
    if sid not in conn.subscriptions:
        await _dispatch(conn, "session.resume", {"session_id": sid})


def _translate_create(params: dict) -> dict:
    if params.get("messages"):
        raise AuthorityError("unsupported", "Seeded sessions (transcript forks) are not available on the "
                                            "session authority yet; use a portable handoff.")
    out: dict = {"source": "gui", "request_id": str(params.get("idempotency_key") or uuid.uuid4().hex)}
    for k in ("provider", "model", "cwd", "cwd_explicit", "title"):
        if params.get(k) not in (None, ""):
            out[k] = params[k]
    if params.get("reasoning_effort"):
        out["reasoning"] = params["reasoning_effort"]
    if out.get("cwd_explicit") and not out.get("cwd"):
        out.pop("cwd_explicit")
    return out


async def _call(method: str, params: dict, home: Any, subject: Optional[str]) -> dict:
    sid = str(params.get("session_id") or "")
    authority = None
    if sid:
        authority, sid = locate(sid)
        if authority is None:
            raise AuthorityError("not_found", f"unknown session {params.get('session_id')}")
    if authority is None:
        authority = for_home(home) if home is not None else launch()
    if authority is None:
        raise AuthorityError("profile_unavailable", "That profile is not served by this Hermes gateway.")
    if subject is None and sid:
        live = authority.sessions.get(sid)
        subject = getattr(getattr(live, "source", None), "user_id", None)
    conn = await _connection(authority, str(subject or default_subject()))

    if method == "session.create":
        return await _dispatch(conn, "session.create", _translate_create(params))
    if method == "session.resume":
        return await _dispatch(conn, "session.resume", {"session_id": sid})
    if method in ("session.close", "session.title"):
        return {}  # the authority keeps child rows open; titles are set at creation
    if method == "model.options":
        return await _dispatch(conn, "model.options", {k: v for k, v in params.items() if k != "profile"})
    if method == "prompt.submit":
        await _subscribed(conn, sid)
        p = {"session_id": sid, "text": str(params.get("text") or ""),
             "submission_id": str(params.get("submission_id") or f"t3o-{uuid.uuid4().hex}")}
        if params.get("turn_author"):
            p["turn_author"] = params["turn_author"]
        return await _dispatch(conn, "prompt.submit", p)
    if method in ("session.interrupt", "session.steer", "session.redirect"):
        await _subscribed(conn, sid)
        h = handle(authority, sid)
        if h.execution_state != "running":
            return {"interrupted": False, "status": "idle"}
        p = {"session_id": sid, "execution_generation": h.execution_generation}
        if method != "session.interrupt":
            p["text"] = str(params.get("text") or "")
        res = await _dispatch(conn, method, p)
        return {"interrupted": True, **res} if method == "session.interrupt" else res
    if sid:
        await _subscribed(conn, sid)
    return await _dispatch(conn, method, {k: v for k, v in params.items() if k != "profile"})


def call(method: str, params: Optional[dict] = None, *, home: Any = None, subject: Optional[str] = None,
         timeout: float = 120.0) -> dict:
    return run(_call(method, dict(params or {}), home, subject), timeout=timeout)


def emit(event: str, sid: str, payload: dict) -> None:
    a, sid = locate(sid)
    live = a.sessions.get(sid) if a is not None else None
    if live is not None:
        with contextlib.suppress(Exception):
            live.event_stream.publish(sid, payload, event_type=event)


# ── observation ─────────────────────────────────────────────────────────────

_tap_sink: dict = {"fn": None}
_observers: dict = {"interrupt": {}, "submit": {}}


def install_tap(sink: Callable[[str, str, dict], None]) -> bool:
    _tap_sink["fn"] = sink
    try:
        from gateway.session_events import SessionEvents
    except Exception:
        return False
    if getattr(SessionEvents.publish, MARK, False):
        return True
    original = SessionEvents.publish

    def publish(self, session_id, payload, *, event_type="message.complete", execution=None):
        delivered = original(self, session_id, payload, event_type=event_type, execution=execution)
        try:
            note_loop()
            fn = _tap_sink["fn"]
            if fn is not None:
                fn(str(event_type or ""), str(session_id or ""), payload if isinstance(payload, dict) else {})
        except Exception:
            logger.debug("t3o authority tap failed", exc_info=True)
        return delivered

    setattr(publish, MARK, True)
    publish.__wrapped__ = original  # type: ignore[attr-defined]
    SessionEvents.publish = publish
    return True


def wrap_method(name: str, before: Optional[Callable[[dict], None]] = None,
                after: Optional[Callable[[dict, Any], None]] = None) -> bool:
    """Observe the Desktop user's ``session.interrupt`` (Stop) and ``prompt.submit``."""
    attr = {"session.interrupt": "interrupt", "prompt.submit": "submit"}.get(name)
    if attr is None:
        return False
    _observers[attr] = {"before": before, "after": after}
    try:
        from gateway.session_controls import AuthorityConnection
    except Exception:
        return False
    current = getattr(AuthorityConnection, attr)
    if getattr(current, MARK, False):
        return True

    async def handler(self, ref, params, _original=current, _attr=attr):
        hooks = _observers[_attr]
        mine = getattr(self, "_t3o", False)
        view = {"session_id": getattr(ref, "session_id", ""), **(params if isinstance(params, dict) else {})}
        if not mine and hooks.get("before") is not None:
            with contextlib.suppress(Exception):
                await asyncio.to_thread(hooks["before"], view)
        result = await _original(self, ref, params)
        if not mine and hooks.get("after") is not None:
            with contextlib.suppress(Exception):
                await asyncio.to_thread(hooks["after"], view, {"result": result})
        return result

    setattr(handler, MARK, True)
    handler.__wrapped__ = current  # type: ignore[attr-defined]
    setattr(AuthorityConnection, attr, handler)
    return True
