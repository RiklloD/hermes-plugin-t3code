"""Orchestrator children in Hermes's native in-chat Subagents frame (above the composer).

The Desktop frame is fed by ``subagent.*`` events on the parent session and reconciled every few
seconds from ``subagent.list``; its Steer / Stop / transcript controls call ``subagent.steer``,
``subagent.interrupt`` and ``subagent.tail``. Orchestrator tasks join that same surface: they are
merged into the roster, answered by those RPCs for their own ids, and announced with live events
that carry the child thread (``child_session_id``) and the final summary. No separate UI.

Native runtimes (Muse Code, Antigravity) report their own subagents here too, like T3 Code's
orchestration V2 projects a provider's native subagents as child work: :func:`native_update` keeps a
``native_*`` row per child with the provider's own Stop / Steer controls.
"""

from __future__ import annotations

import contextlib
import importlib
import threading
import time
from typing import Any, Callable, Optional

from . import gw, handoff
from .store import TERMINAL, loads

_STATUS = {"starting": "queued", "running": "running", "waiting": "running", "waiting_children": "running",
           "completed": "completed", "failed": "failed", "cancelled": "interrupted", "interrupted": "interrupted"}
RECENT_S = 15 * 60  # finished tasks stay in the roster this long so their outcome is visible


def _label(eng, t: dict) -> str:
    model = " · ".join(x for x in (t.get("provider"), t.get("model")) if x)
    if eng._pname(t.get("profile")) != eng._pname(t.get("parent_profile") or t.get("profile")):
        model = f"{model} · as {eng._pname(t.get('profile'))}" if model else f"as {eng._pname(t.get('profile'))}"
    return model


def snapshot(eng, t: dict) -> dict:
    """``SubagentSnapshot`` (exact contract fields) for one orchestrator task."""
    progress = loads(t.get("progress"), {}) or {}
    live = eng.live.get(t["id"]) or {}
    return {
        "subagent_id": t["id"], "parent_id": None, "depth": 0, "goal": t["title"], "delegation_id": t["id"],
        "model": _label(eng, t) or None, "started_at": t.get("started_at") or t["created_at"],
        "status": _STATUS.get(t["status"], "running"), "tool_count": int(t.get("tool_count") or 0),
        "last_tool": live.get("tool") or progress.get("tool"),
        "accepting_steer": t["status"] not in TERMINAL,
    }


def event_payload(eng, t: dict, **extra: Any) -> dict:
    end = t.get("finished_at") or time.time()
    payload = {
        "goal": t["title"], "task_count": 1, "task_index": 0, "subagent_id": t["id"], "parent_id": None,
        "child_session_id": t.get("child_key"), "delegation_id": t["id"], "depth": 0,
        "model": _label(eng, t) or None, "tool_count": int(t.get("tool_count") or 0),
        "status": _STATUS.get(t["status"], "running"),
    }
    if t["status"] in TERMINAL:
        body = t.get("result") or t.get("error") or ""
        if t["status"] != "completed" and t.get("error"):
            body = f"{t['error']}\n\n{t.get('result') or ''}".strip()
        payload.update(summary=body[:4000] or None,
                       duration_seconds=round(end - (t.get("started_at") or t["created_at"]), 1))
    payload.update({k: v for k, v in extra.items() if v is not None})
    return payload


def _parent_sids(eng, t: dict) -> list[str]:
    sid, _ = eng.live_session(t["parent_key"], t.get("parent_profile"))
    return [sid] if sid else []


def announce(eng, t: Optional[dict], event: str, **extra: Any) -> None:
    """Push one ``subagent.*`` frame to every window showing the parent chat."""
    if not t:
        return
    payload = event_payload(eng, t, **extra)
    for sid in _parent_sids(eng, t):
        gw.emit(event, sid, payload)


# ── native subagents (reported by native runtimes) ────────────────────────────────────────────

NATIVE: dict[str, dict] = {}
_native_lock = threading.Lock()


def _native_payload(n: dict, **extra: Any) -> dict:
    payload = {"goal": n["goal"], "task_count": 1, "task_index": 0, "subagent_id": n["id"], "parent_id": None,
               "child_session_id": None, "delegation_id": n["id"], "depth": 0, "model": n.get("model"),
               "tool_count": int(n.get("tool_count") or 0), "status": n["status"]}
    if n["status"] in ("completed", "failed", "interrupted"):
        payload.update(summary=(n.get("summary") or "")[:4000] or None,
                       duration_seconds=round((n.get("finished_at") or time.time()) - n["started_at"], 1))
    payload.update({k: v for k, v in extra.items() if v is not None})
    return payload


def native_update(session_key: str, sub: dict, *, stop: Optional[Callable[[], bool]] = None,
                  steer: Optional[Callable[[str], bool]] = None) -> None:
    """Add or update one native subagent of the chat ``session_key`` and announce the change.

    ``sub``: ``id`` (stable per child), ``goal``, ``status`` (queued / running / completed / failed /
    interrupted), optional ``model``, ``tool_count``, ``last_tool``, ``summary``, ``transcript``.
    """
    eng = importlib.import_module("t3o.engine").engine()
    sid = f"native_{sub['id']}"
    with _native_lock:
        prior = NATIVE.get(sid)
        n = {**(prior or {"started_at": time.time()}), **{k: v for k, v in sub.items() if v is not None}, "id": sid,
             "session_key": session_key, "parent_key": eng.root(session_key)}
        if stop is not None:
            n["stop"] = stop
        if steer is not None:
            n["steer"] = steer
        terminal = n["status"] in ("completed", "failed", "interrupted")
        if terminal and not (prior or {}).get("finished_at"):
            n["finished_at"] = time.time()
        NATIVE[sid] = n
        # Forget long-finished children so the registry stays small.
        for key in [k for k, v in NATIVE.items() if (v.get("finished_at") or time.time()) < time.time() - RECENT_S]:
            NATIVE.pop(key, None)
    if prior is None:
        event = "subagent.start"
    elif terminal and prior.get("status") not in ("completed", "failed", "interrupted"):
        event = "subagent.complete"
    elif n.get("last_tool") and n.get("last_tool") != prior.get("last_tool"):
        event = "subagent.tool"
    elif n["status"] != prior.get("status"):
        event = "subagent.progress"
    else:
        return
    sid_live, _ = eng.live_session(session_key)
    if sid_live:
        gw.emit(event, sid_live, _native_payload(n, tool_name=n.get("last_tool")))


def _native_snapshot(n: dict) -> dict:
    return {"subagent_id": n["id"], "parent_id": None, "depth": 0, "goal": n["goal"], "delegation_id": n["id"],
            "model": n.get("model"), "started_at": n["started_at"], "status": n["status"],
            "tool_count": int(n.get("tool_count") or 0), "last_tool": n.get("last_tool"),
            "accepting_steer": n["status"] in ("queued", "running") and n.get("steer") is not None}


def _native_for(eng, params: dict) -> Optional[dict]:
    n = NATIVE.get(str(params.get("subagent_id") or ""))
    if n is None:
        return None
    key, _ = eng._key_of_sid(str(params.get("session_id") or ""))
    return n if not key or key == n["parent_key"] else None


def _task_for(eng, params: dict) -> tuple[Optional[dict], str]:
    task_id = str(params.get("subagent_id") or "")
    if not task_id.startswith("task_"):
        return None, task_id
    t = eng.store.task(task_id)
    if t is None:
        return None, task_id
    key, _ = eng._key_of_sid(str(params.get("session_id") or ""))
    # Only the parent chat (or an ancestor's) may control its children from the frame.
    if key and key not in (t["parent_key"], t["root_key"]):
        return None, task_id
    return t, task_id


def install(eng) -> None:
    def list_after(params: dict, resp: Any) -> None:
        if not isinstance(resp, dict) or not isinstance(resp.get("result"), dict):
            return
        key, _ = eng._key_of_sid(str(params.get("session_id") or ""))
        if not key:
            return
        now = time.time()
        rows = [snapshot(eng, t) for t in eng.store.tasks_for_parent(key)
                if t["status"] not in TERMINAL or (t.get("finished_at") or 0) > now - RECENT_S]
        rows += [_native_snapshot(n) for n in list(NATIVE.values()) if n["parent_key"] == key and
                 (n["status"] in ("queued", "running") or (n.get("finished_at") or 0) > now - RECENT_S)]
        if rows:
            resp["result"].setdefault("subagents", []).extend(rows)

    def interrupt(rid, params):
        n = _native_for(eng, params)
        if n is not None:
            found = n["status"] in ("queued", "running") and n.get("stop") is not None
            if found:
                with contextlib.suppress(Exception):
                    found = bool(n["stop"]())
            return gw.ok(rid, {"found": found, "subagent_id": n["id"]})
        t, task_id = _task_for(eng, params)
        if t is None:
            return None
        found = t["status"] not in TERMINAL
        if found:
            eng.cancel(t["id"], "stopped from the chat")
        return gw.ok(rid, {"found": found, "subagent_id": task_id})

    def steer(rid, params):
        n = _native_for(eng, params)
        if n is not None:
            text, status = str(params.get("text") or ""), "rejected"
            if n.get("steer") is not None and text.strip() and n["status"] in ("queued", "running"):
                with contextlib.suppress(Exception):
                    status = "queued" if n["steer"](text) else "rejected"
            return gw.ok(rid, {"status": status, "subagent_id": n["id"], "text": text})
        t, task_id = _task_for(eng, params)
        if t is None:
            return None
        text = str(params.get("text") or "")
        status = "rejected"
        sid, s = eng.live_session(t.get("child_key") or "", t.get("profile"))
        if sid and gw.is_running(s) and text.strip():
            with contextlib.suppress(Exception):
                status = "queued" if eng._steer(sid, s, text, "steer") != "rejected" else "rejected"
        elif t["status"] not in TERMINAL and text.strip():
            eng.queue_add(t["child_key"], text, t.get("profile"), created_by="user")
            status = "queued"
        return gw.ok(rid, {"status": status, "subagent_id": task_id, "text": text})

    def tail(rid, params):
        n = _native_for(eng, params)
        if n is not None:
            text = "\n\n".join(x for x in (f"Task: {n['goal']}", n.get("transcript") or "", n.get("summary") or "") if x)
            return gw.ok(rid, {"subagent_id": n["id"], "available": bool(text), "text": text[-16384:],
                               "truncated": len(text) > 16384})
        t, task_id = _task_for(eng, params)
        if t is None:
            return None
        text = ""
        with contextlib.suppress(Exception):
            msgs = gw.history(eng.tip(t["child_key"], t.get("profile")), t.get("profile"))
            text = "\n\n".join(handoff._items(msgs))
        live = (eng.live.get(t["id"]) or {}).get("tail")
        if live and t["status"] not in TERMINAL:
            text = f"{text}\n\n[streaming]\n{live}" if text else live
        truncated = len(text.encode("utf-8")) > 16384
        if truncated:
            text = text.encode("utf-8")[-16384:].decode("utf-8", "ignore")
        return gw.ok(rid, {"subagent_id": task_id, "available": bool(text), "text": text, "truncated": truncated})

    gw.wrap_method("subagent.list", after=list_after)
    gw.intercept_method("subagent.interrupt", interrupt)
    gw.intercept_method("subagent.steer", steer)
    gw.intercept_method("subagent.tail", tail)
