"""In-thread harness switching: T3's "change provider mid-thread" for Hermes.

A thread can start on any Hermes-loop model and continue on a native runtime (Claude CLI), and back,
without leaving the chat. Hermes refuses both directions on its own: ``AIAgent.switch_model`` cannot
cross the native boundary in place, and the native host refuses to start a runtime on a transcript it
did not produce. Two runtime wraps lift that, with no Hermes core edits:

- ``server._commit_agent_switch``: a model pick that crosses the boundary rebuilds the session's agent
  on the new provider (same session, same history) instead of swapping it in place.
- ``agent.native_runtime._ensure_runtime``: when the native runtime meets turns it never saw, it gets
  a T3-style portable handoff on its first turn. A thread with no native session yet (or one that moved
  workspace, was rewound or copied) starts a fresh native session seeded with the whole transcript; a
  thread the native runtime already owns resumes that session with only the turns it missed.

Every handoff is stored in ``transfers`` (type ``harness_switch``) for audit, like forks and merges.
"""

from __future__ import annotations

import contextlib
import importlib
import logging
import threading
from typing import Any, Optional

from . import gw, handoff

logger = logging.getLogger("t3o.harness")

STATE_KEY = "native_agent_runtime"
MARK = "_t3o_harness"
# session key -> (provider, model) the thread ran on just before a cross-harness switch.
_sources: dict[str, tuple[str, str]] = {}
_lock = threading.Lock()


def _store():
    return importlib.import_module("t3o.engine").engine().store


def _enabled() -> bool:
    with contextlib.suppress(Exception):
        return bool(_store().setting("harness_switch"))
    return True


def _native(provider: str) -> bool:
    with contextlib.suppress(Exception):
        from agent.native_runtime import runtime_profile
        return bool(runtime_profile(provider))
    return False


def _label(provider: str, model: str) -> str:
    name = provider or "another provider"
    with contextlib.suppress(Exception):
        from providers import get_provider_profile
        profile = get_provider_profile(provider)
        name = getattr(profile, "display_name", "") or name
    return f"{name} / {model}" if model else name


# ── model switch across the native boundary ─────────────────────────────────

def crosses(old_provider: str, new_provider: str) -> bool:
    return old_provider != new_provider and (_native(old_provider) or _native(new_provider))


def install_switch_wrap() -> bool:
    srv = gw.server()
    current = getattr(srv, "_commit_agent_switch", None) if srv is not None else None
    if current is None:
        return False
    if getattr(current, MARK, False):
        return True

    def commit(sid, session, agent, result, current_model, snapshot):
        if not (_enabled() and crosses(str(getattr(agent, "provider", "") or ""), str(result.target_provider or ""))):
            return current(sid, session, agent, result, current_model, snapshot)
        return _switch_harness(srv, sid, session, agent, result, snapshot)

    setattr(commit, MARK, True)
    commit.__wrapped__ = current  # type: ignore[attr-defined]
    srv._commit_agent_switch = commit
    return True


def _switch_harness(srv, sid: str, session: dict, agent, result, snapshot) -> None:
    """Rebuild the session's agent on the other harness; the transcript stays the session's own."""
    if snapshot is not None:
        raise ValueError("A one-turn (--once) switch cannot change harness. Switch for the session instead.")
    old_provider, old_model = str(agent.provider or ""), str(agent.model or "")
    key = str(session.get("session_key") or agent.session_id)
    override = {"model": result.new_model, "provider": result.target_provider, "base_url": result.base_url,
                "api_key": result.api_key, "api_mode": result.api_mode}
    tokens = srv._set_session_context(key, cwd=srv._session_cwd(session))
    try:
        new_agent = srv._rebuild_session_agent(sid, session, session_id=key, model_override=override,
                                               platform_override=srv._session_source(session))
    except Exception as exc:
        logger.warning("t3o: harness switch %s -> %s failed", old_provider, result.target_provider, exc_info=True)
        raise ValueError(f"Model switch to {result.new_model} failed ({exc}); staying on {old_model}.") from exc
    finally:
        srv._clear_session_context(tokens)
    with _lock:
        _sources[str(new_agent.session_id)] = (old_provider, old_model)
    if _native(old_provider):
        # The native session stops seeing this thread; it gets the missed turns if the thread comes back.
        with contextlib.suppress(Exception):
            from agent.native_runtime import _state
            if (_state(new_agent) or {}).get("session_id"):
                _patch_state(new_agent, {"detached": True})
        threading.Thread(target=_close_runtime, args=(agent,), name="t3o-harness-close", daemon=True).start()
    srv._restart_slash_worker(sid, session)
    srv._persist_live_session_runtime(session)
    srv._persist_live_session_system_prompt(session)
    srv._append_model_switch_marker(session, model=result.new_model, provider=result.target_provider)
    srv._emit_session_info(sid, session)
    session.pop("one_turn_model_restore", None)
    logger.info("t3o: %s switched harness %s/%s -> %s/%s", key, old_provider, old_model,
                result.target_provider, result.new_model)


def _close_runtime(agent) -> None:
    with contextlib.suppress(Exception):
        from agent.native_runtime import close_runtime
        close_runtime(agent)


# ── native runtime meeting a transcript it did not produce ──────────────────

def install_runtime_wrap() -> bool:
    try:
        nr = importlib.import_module("agent.native_runtime")
    except Exception:
        return False
    current = getattr(nr, "_ensure_runtime", None)
    if current is None:
        return False
    if getattr(current, MARK, False):
        return True

    def ensure(agent, messages):
        plan = None
        if _enabled():
            try:
                plan = plan_handoff(nr._state(agent), agent, messages)
            except Exception:
                logger.warning("t3o: harness handoff planning failed", exc_info=True)
        if plan is None:
            return current(agent, messages)
        if plan["reset"]:
            _reset_state(agent)
            runtime = current(agent, [])
        else:
            _patch_state(agent, {"detached": None})
            runtime = current(agent, messages)
        if plan["text"]:
            arm(runtime, plan["text"])
            _record(agent, plan)
        return runtime

    setattr(ensure, MARK, True)
    ensure.__wrapped__ = current  # type: ignore[attr-defined]
    nr._ensure_runtime = ensure
    return True


def _cwd(agent) -> str:
    from agent.runtime_cwd import resolve_agent_cwd
    return str(getattr(agent, "session_cwd", None) or resolve_agent_cwd())


def _tail_index(messages: list[dict], uid: Any) -> Optional[int]:
    if not uid:
        return None
    for i in range(len(messages) - 1, -1, -1):
        if messages[i].get("message_uid") == uid:
            return i
    return None


def plan_handoff(state: dict, agent, messages: list[dict], *, cwd: Optional[str] = None) -> Optional[dict]:
    """None = let the host behave as before. Otherwise ``{reset, text, strategy, source, count}``."""
    state = state if isinstance(state, dict) else {}
    messages = list(messages or [])
    provider, model = str(agent.provider or ""), str(agent.model or "")
    target = _label(provider, model)
    with _lock:
        prev = _sources.get(str(agent.session_id))
    source = _label(*prev) if prev and prev[0] != provider else "another model in Hermes"
    budget = 160_000
    with contextlib.suppress(Exception):
        budget = int(_store().setting("harness_handoff_bytes"))

    def full(reason: str) -> dict:
        text = (handoff.thread_context(messages, source=source, target=target, budget=budget)
                if handoff.has_content(messages) else None)
        return {"reset": True, "text": text, "strategy": "portable_context", "reason": reason,
                "source": prev, "count": len(messages)}

    if not state:
        # A fresh chat stays the host's normal first turn; history it never saw needs a handoff.
        return full("hermes_transcript") if any(m.get("role") == "assistant" for m in messages) else None
    if state.get("fork_from") and not state.get("session_id"):
        return None  # an orchestrator native fork that has not started yet: Claude forks it natively
    cwd = cwd if cwd is not None else _cwd(agent)
    if state.get("owner") and state["owner"] != agent.session_id:
        return full("copied_session")
    if state.get("provider") and state["provider"] != provider:
        return full("other_runtime")
    if state.get("cwd") and state["cwd"] != cwd:
        return full("workspace_changed")
    tail = state.get("mirror_tail_uid")
    idx = _tail_index(messages, tail)
    if tail and idx is None:
        return full("rewound")
    after = messages[idx + 1:] if idx is not None else []
    if not after or not handoff.has_content(after):
        return None
    # A completed native turn always advances the tail, so a completed run after it came from another
    # harness. ``detached`` covers a switch away whose turns all failed or were stopped.
    if not (state.get("detached") or any(r["status"] == "completed" for r in handoff.runs(after))):
        return None
    text = handoff.thread_context(after, source=source, target=target, budget=budget, delta=True)
    return {"reset": False, "text": text, "strategy": "native_resume_delta", "reason": "missed_turns",
            "source": prev, "count": len(after)}


def _db(agent):
    return getattr(agent, "_session_db", None)


def _patch_state(agent, patch: dict) -> None:
    from agent.native_runtime import _state
    state = {k: v for k, v in {**(_state(agent) or {}), **patch}.items() if v is not None}
    agent._native_runtime_state = state
    db = _db(agent)
    if db is not None and state:
        db.patch_session_model_config(agent.session_id, {STATE_KEY: state})


def _reset_state(agent) -> None:
    """Retire the thread's native session identity (it stays resumable in Claude's own history)."""
    if getattr(agent, "_native_runtime", None) is not None:
        _close_runtime(agent)
    agent._native_runtime_state = {}
    db = _db(agent)
    if db is not None:
        db.patch_session_model_config(agent.session_id, {STATE_KEY: None})


def prefix(message: Any, text: str) -> Any:
    if isinstance(message, list):
        return [{"type": "text", "text": text + "\n\n[Current message]"}, *message]
    return f"{text}\n\n[Current message]\n{message}"


def arm(runtime, text: str) -> None:
    """The next turn (only) carries the handoff ahead of the user's message; the display mirror does not."""
    original = runtime.run_turn

    def run_turn(message, *, model):
        with contextlib.suppress(Exception):
            del runtime.run_turn
        return original(prefix(message, text), model=model)

    runtime.run_turn = run_turn


def _record(agent, plan: dict) -> None:
    with contextlib.suppress(Exception):
        sid, session = gw.session_for_agent(agent)
        profile = gw.profile_of(session)
        eng = importlib.import_module("t3o.engine").engine()
        key = eng.root(str(agent.session_id), profile)
        src = plan.get("source") or ("", "")
        from .store import dumps, new_id
        import time
        eng.store.insert("transfers", {
            "id": new_id("xfer"), "type": "harness_switch", "source_key": key, "target_key": key,
            "profile": profile, "source_point": dumps({"messages": plan["count"], "reason": plan["reason"]}),
            "source_provider": src[0] or None, "target_provider": str(agent.provider or ""),
            "strategy": plan["strategy"], "status": "consumed", "delivery": "next_message",
            "handoff": plan["text"], "created_by": "user", "created_at": time.time(), "consumed_at": time.time(),
        })
        eng.store.log("transfer.harness_switch", key, plan["strategy"])
    with contextlib.suppress(Exception):
        fn = getattr(agent, "_emit_diagnostic_status", None)
        if callable(fn):
            what = "the turns it missed" if plan["strategy"] == "native_resume_delta" else "this thread's context"
            fn(f"Handed {what} ({plan['count']} messages) to {_label(str(agent.provider), str(agent.model))}.")
