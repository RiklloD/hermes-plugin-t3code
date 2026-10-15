"""Orchestrator V2 for Hermes: the process-wide engine inside ``hermes serve``.

Feature map (T3 Code orchestration V2 → Hermes):

* Agents delegate work — ``delegate`` creates a real child session on any provider/model the
  profile can reach, sends it a prompt-only brief, and returns immediately (or waits). The
  child's terminal result is delivered back to the parent as a new turn (a delegation card)
  once the parent is idle; ``task_status`` reads it any time.
* Visible background work — child progress (model, status, tools, last output, duration) is
  observed from the session event stream and published to the Desktop pane. A user Stop on
  the parent also stops every descendant task, even after the parent's own turn finished.
* Fork and merge back — forks branch from any run (failed or interrupted ones included):
  Hermes-loop sessions copy their own transcript; Claude CLI sessions fork natively (the
  Claude session is forked, not replayed); other targets receive an explicit portable
  context handoff. Merge-back delivers the fork's delta to the source thread.
* Steer or queue — a durable per-session queue (edit, reorder, remove, promote to steer)
  that survives restarts and drains when the session is idle; steering uses Hermes's
  in-turn steer, restart uses redirect.
* Recovery and automation — continuation after backend restarts (update, quit or crash;
  T3's RestartContinuation, policy in ``restart.py``), automatic resume when a provider usage
  limit resets, and scheduled tasks (interval, fixed time, webhook) bound to a session or
  launching fresh ones.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import logging
import queue
import sys
import threading
import time
from pathlib import Path
from typing import Any, Optional

from . import chatframe, gw, handoff, harness, restart, schedule as sched
from .store import TERMINAL, Store, dumps, loads, new_id

# Native runtimes that fork their own session (Claude CLI: fork_session; Muse Code: session/fork).
# Antigravity's ACP agent has no fork, so its forks carry a portable handoff like any native target.
NATIVE_FORK_PROVIDERS = ("claude-cli", "claude-cli-2", "muse-code")
logger = logging.getLogger("t3o.engine")

# Desktop events go out as ``plugin.t3code.orchestrator.<event>`` (one plugin, one namespace per feature).
PLUGIN_ID = "t3code"
EVENT_PREFIX = "orchestrator."
CONTINUE_PROMPT = restart.CONTINUE_PROMPT
CARD = "async_delegation_complete"  # Desktop renders this kind as a compact completion card
ROLES = ("implementation", "research", "review", "design", "test", "general")
_ROLE_HINT = {
    "implementation": "Make the change end to end, run the relevant checks, and report what changed.",
    "research": "Investigate and report findings with sources/evidence. Do not modify files unless asked.",
    "review": "Review critically. Report concrete problems with file:line references, severity, and suggested fixes. "
              "Do not modify files unless asked.",
    "design": "Propose a design with trade-offs and a recommendation. Do not modify files unless asked.",
    "test": "Write or run tests as asked and report exact results (commands, pass/fail, failures).",
    "general": "Complete the task and report the outcome.",
}


class OrchestratorError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code

    def to_dict(self) -> dict:
        return {"ok": False, "error": self.code, "message": str(self)}


def _now() -> float:
    return time.time()


def _clip(text: Any, n: int) -> str:
    s = "" if text is None else str(text)
    return s if len(s) <= n else s[: n - 1] + "…"


class Engine:
    def __init__(self, store: Optional[Store] = None) -> None:
        self.store = store or Store()
        self.events: "queue.Queue[tuple[str, str, dict]]" = queue.Queue(maxsize=20000)
        self.waiters: dict[str, threading.Event] = {}
        self.live: dict[str, dict] = {}  # task_id → transient progress (tail text, last tool, …)
        self._last_flush: dict[str, float] = {}
        self._started = False
        self._owner = False
        self._lock = threading.RLock()
        self._thread: Optional[threading.Thread] = None
        self._boot_done = False
        self._started_at = _now()
        self._lock_file = None
        self._last_tick = 0.0
        self._last_reconcile = 0.0
        self._roots: dict[str, str] = {}
        self._profiles_loaded = 0
        self._paused = False
        self._enforced: dict[str, float] = {}
        self._dispatch_lock = threading.RLock()  # serializes claim → dispatch against Stop / cancel
        self._backoff: dict[str, float] = {}
        self._fails: dict[str, int] = {}
        self._live_written: dict[str, str] = {}  # session key → signature of this process's live_work row
        self._last_live = 0.0
        self._shutting_down = False
        self._identity: Optional[tuple[int, Optional[float]]] = None
        self.webhooks = sched.WebhookListener(self._on_webhook)

    # ── lifecycle ───────────────────────────────────────────────────────────
    def attach_profile(self) -> None:
        """A profile's plugin manager registered the plugin; the engine runs while any profile has it."""
        with self._lock:
            self._profiles_loaded += 1
            self._paused = False
        self.start()

    def detach_profile(self) -> None:
        """on_unload: when the last profile drops the plugin, stop side effects (no ticks, no dispatch)."""
        with self._lock:
            self._profiles_loaded = max(0, self._profiles_loaded - 1)
            if self._profiles_loaded:
                return
            self._paused = True
        with contextlib.suppress(Exception):
            self.webhooks.stop()
        if self._owner and self._lock_file is not None:
            with contextlib.suppress(Exception):
                fcntl.flock(self._lock_file.fileno(), fcntl.LOCK_UN)
                self._lock_file.close()
            self._lock_file, self._owner = None, False
        logger.info("t3code orchestrator engine paused (plugin unloaded)")

    def start(self) -> None:
        with self._lock:
            if self._started:
                return
            self._started = True
        self._thread = threading.Thread(target=self._main, name="t3o-engine", daemon=True)
        self._thread.start()

    def _claim_owner(self) -> bool:
        """One engine per machine runs side effects (deliveries, queue drains, schedules)."""
        if self._owner:
            return True
        path = self.store.path.parent / "engine.lock"
        try:
            fh = open(path, "a+")
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return False
        self._lock_file, self._owner = fh, True
        return True

    def _main(self) -> None:
        # The gateway module appears once the backend finishes importing; plugins can load first.
        while not gw.is_serve_process():
            time.sleep(1.0)
            if _now() - self._started_at > 3600:
                return
        gw.install_tap(self._tap)
        gw.wrap_method("session.interrupt", before=self._on_user_stop)
        gw.wrap_method("prompt.submit", after=self._on_user_prompt)
        chatframe.install(self)  # children live in the native in-chat Subagents frame
        harness.install_switch_wrap()  # Hermes model <-> Claude CLI in the same thread
        harness.install_runtime_wrap()
        # T3 prepareForShutdown: capture restart intent before core's exit path interrupts the turns.
        gw.wrap_global("_stop_turns_before_exit", self.prepare_for_shutdown)
        logger.info("t3code orchestrator engine attached (owner=%s)", self._claim_owner())
        while True:
            try:
                kind, sid, payload = self.events.get(timeout=1.0)
                self._handle(kind, sid, payload)
                while True:
                    try:
                        kind, sid, payload = self.events.get_nowait()
                    except queue.Empty:
                        break
                    self._handle(kind, sid, payload)
            except queue.Empty:
                pass
            except Exception:
                logger.warning("t3o event handling failed", exc_info=True)
            if _now() - self._last_live >= 2.0:
                self._last_live = _now()
                try:
                    self._record_live_work()
                except Exception:
                    logger.debug("t3o live-work snapshot failed", exc_info=True)
            if _now() - self._last_tick >= 2.0:
                self._last_tick = _now()
                try:
                    self.tick()
                except Exception:
                    logger.warning("t3o tick failed", exc_info=True)

    def _tap(self, kind: str, sid: str, payload: dict) -> None:
        # Once shutdown starts, core interrupts every turn: those frames are the restart, not outcomes.
        if self._paused or self._shutting_down:
            return
        if kind in ("message.start", "message.complete", "tool.start", "tool.complete", "message.delta",
                    "todo.updated", "error", "session.title"):
            with contextlib.suppress(queue.Full):
                self.events.put_nowait((kind, sid, payload if isinstance(payload, dict) else {}))

    def broadcast(self, event: str, payload: dict) -> None:
        # Only a Desktop backend has clients; importing hermes_cli from a bare process can re-exec it.
        if not gw.is_serve_process():
            return
        with contextlib.suppress(Exception):
            from hermes_cli.plugin_events import broadcast_plugin_event
            broadcast_plugin_event(PLUGIN_ID, EVENT_PREFIX + event, payload)

    # ── event handling ──────────────────────────────────────────────────────
    def _handle(self, kind: str, sid: str, payload: dict) -> None:
        if self._shutting_down:
            return
        key, profile = self._key_of_sid(sid)
        if not key:
            return
        task = self.store.task_for_child(key)
        if kind == "message.start":
            self.store.upsert_turn(key, status="running", started_at=_now(), profile=profile)
            if task and task["status"] in ("starting", "waiting", "waiting_children") and not task["cancel_requested"]:
                if self.store.update("tasks", task["id"], {"status": "running", "started_at": task["started_at"] or _now()},
                                     where="status IN ('starting','waiting','waiting_children') AND cancel_requested = 0"):
                    self._task_changed(task["id"])
            return
        if kind == "message.complete":
            self._on_turn_complete(sid, key, profile, task, payload)
            return
        if not task or task["status"] in TERMINAL:
            return
        live = self.live.setdefault(task["id"], {})
        if kind == "tool.start":
            live["tool"] = payload.get("name")
            live["tool_hint"] = _clip(payload.get("context") or payload.get("preview") or "", 140)
            live["tool_count"] = int(live.get("tool_count") or task.get("tool_count") or 0) + 1
            live["at"] = _now()
            chatframe.announce(self, {**task, "tool_count": live["tool_count"]}, "subagent.tool",
                               tool_name=live["tool"], tool_preview=live["tool_hint"] or None)
        elif kind == "tool.complete":
            live["tool_done"] = payload.get("name")
            live["at"] = _now()
        elif kind == "message.delta":
            text = payload.get("text")
            if isinstance(text, str) and text:
                live["tail"] = (str(live.get("tail") or "") + text)[-600:]
                live["at"] = _now()
        elif kind == "todo.updated":
            todos = payload.get("todos") or []
            done = sum(1 for t in todos if isinstance(t, dict) and t.get("status") == "completed")
            live["todos"] = f"{done}/{len(todos)} todos"
        elif kind == "session.title":
            # Child sessions keep the task title the parent chose.
            title = str(payload.get("title") or "")
            want = task.get("child_title") or self._child_title(task)
            if title and title != want:
                with contextlib.suppress(Exception):
                    gw.call("session.title", {"session_id": sid, "title": want})
            return
        self._flush_progress(task["id"])

    def _flush_progress(self, task_id: str, force: bool = False) -> None:
        live = self.live.get(task_id) or {}
        if not force and _now() - self._last_flush.get(task_id, 0) < 1.0:
            return
        self._last_flush[task_id] = _now()
        progress = {k: live.get(k) for k in ("tool", "tool_hint", "tail", "todos", "at") if live.get(k)}
        self.store.update("tasks", task_id, {"progress": dumps(progress), "tool_count": int(live.get("tool_count") or 0)})
        self._task_changed(task_id)

    def _on_turn_complete(self, sid: str, key: str, profile: Optional[str], task: Optional[dict], payload: dict) -> None:
        status = str(payload.get("status") or "complete")
        error = payload.get("error") or payload.get("failure_reason")
        surface = payload.get("error_surface") or {}
        text = payload.get("text") if isinstance(payload.get("text"), str) else ""
        self.store.upsert_turn(key, status="error" if status == "error" else "idle", ended_at=_now(),
                               error=_clip(error, 2000) if error else None, profile=profile)
        armed = False
        if status == "error" and isinstance(surface, dict) and isinstance(surface.get("resets_at"), (int, float)):
            armed = self._arm_usage_limit(key, profile, float(surface["resets_at"]), surface)
        self.broadcast("session.turn", {"session_key": key, "status": status})
        if not task or task["status"] in TERMINAL:
            return
        active = "status NOT IN ('completed','failed','cancelled','interrupted') AND cancel_requested = 0"
        if armed and self.store.setting("resume_on_usage_limit") and not task["cancel_requested"]:
            if self.store.update("tasks", task["id"], {"status": "waiting", "limit_reset_at": float(surface["resets_at"]),
                                                       "error": _clip(error or text, 2000)}, where=active):
                self._task_changed(task["id"])
                return
        if status == "complete" and not task["cancel_requested"] and self.store.tasks_for_parent(key, active_only=True):
            # T3: a finished turn with live nested work is not a finished task. Its children's results
            # arrive as a new turn in this child; that turn's completion publishes the task result.
            if self.store.update("tasks", task["id"], {"status": "waiting_children",
                                                       "result": _clip(text or self._last_reply(task), 60_000)}, where=active):
                self._task_changed(task["id"])
                return
        if status == "complete" and not task["cancel_requested"]:
            result = text or self._last_reply(task)
            values = {"status": "completed", "result": _clip(result, 60_000), "error": None}
        elif status in ("interrupted", "complete"):
            values = {"status": "cancelled" if task["cancel_requested"] else "interrupted",
                      "result": _clip(text or self._last_reply(task), 60_000) or None}
        else:
            values = {"status": "failed", "error": _clip(error or text or "the child turn failed", 4000),
                      "result": _clip(text, 20_000) or None}
        self._finalize(task, values)

    def _last_reply(self, task: dict) -> str:
        with contextlib.suppress(Exception):
            return handoff.last_reply(gw.history(task["child_key"], task["profile"]))
        return ""

    def _finalize(self, task: dict, values: dict) -> None:
        self._flush_progress(task["id"], force=True)
        values = {**values, "finished_at": _now()}
        # Waited tasks are answered inline; the waiter marks delivery itself.
        values["delivery"] = "pending" if task["delivery"] in ("none", "pending") else task["delivery"]
        # Exactly one finalization wins (terminal event vs cancel fallback vs reconcile).
        if not self.store.update("tasks", task["id"], values,
                                 where="status NOT IN ('completed','failed','cancelled','interrupted')"):
            return
        self.store.insert("transfers", {
            "id": new_id("xfer"), "type": "subagent_result", "source_key": task["child_key"] or "",
            "target_key": task["parent_key"], "profile": task.get("parent_profile"), "strategy": "result_card",
            "status": "pending", "delivery": "now", "handoff": None, "created_by": "system", "created_at": _now(),
        })
        self.store.log("task.finished", task["id"], values.get("status"))
        chatframe.announce(self, self.store.task(task["id"]), "subagent.complete")
        if (ev := self.waiters.get(task["id"])) is not None:
            ev.set()
        self._task_changed(task["id"])

    # ── identity across compression ─────────────────────────────────────────
    # Compression rotates a session's stored id (child row, end_reason='compression' on the parent).
    # Orchestration records use the lineage ROOT (the Desktop's durable id); live lookups use the TIP.
    def root(self, key: str, profile: Optional[str] = None) -> str:
        if not key:
            return key
        hit = self._roots.get(key)
        if hit is not None:
            return hit
        cur, seen = key, {key}
        for _ in range(50):
            row = None
            with contextlib.suppress(Exception):
                row = gw.session_row(cur, profile)
            parent = (row or {}).get("parent_session_id")
            if not parent or parent in seen:
                break
            prow = None
            with contextlib.suppress(Exception):
                prow = gw.session_row(parent, profile)
            if (prow or {}).get("end_reason") != "compression":
                break
            seen.add(parent)
            cur = parent
        self._roots[key] = cur
        return cur

    def tip(self, key: str, profile: Optional[str] = None) -> str:
        with contextlib.suppress(Exception):
            db = gw.open_db(profile)
            try:
                return db.get_compression_tip(key) or key
            finally:
                with contextlib.suppress(Exception):
                    db.close()
        return key

    def live_session(self, key: str, profile: Optional[str] = None) -> tuple[Optional[str], Optional[dict]]:
        sid, s = gw.live_for_key(key)
        if sid is None:
            t = self.tip(key, profile)
            if t != key:
                sid, s = gw.live_for_key(t)
        return sid, s

    def ensure_live(self, key: str, profile: Optional[str] = None, *, build: bool = True) -> tuple[str, dict]:
        sid, s = self.live_session(key, profile)
        if sid is not None:
            return sid, s
        return gw.ensure_live(self.tip(key, profile), profile, build=build)

    def _key_of_sid(self, sid: str) -> tuple[str, Optional[str]]:
        """(lineage root, profile) of a live runtime session."""
        key = gw.key_for_sid(sid)
        srv = gw.server()
        session = srv._sessions.get(sid) if srv is not None else None
        profile = gw.profile_of(session) if isinstance(session, dict) else None
        if not key:
            return "", profile
        root = self.root(key, profile)
        if root == key and isinstance(session, dict) and session.get("session_key") and session["session_key"] != key:
            root = self.root(session["session_key"], profile)
        return root, profile

    def _task_changed(self, task_id: str) -> None:
        t = self.store.task(task_id)
        if t:
            self.broadcast("task.updated", self.task_view(t))

    # ── user actions observed on the gateway ────────────────────────────────
    def _on_user_stop(self, params: dict) -> None:
        if self._paused:
            return
        sid = str(params.get("session_id") or "")
        key, profile = self._key_of_sid(sid)
        if not key:
            return
        # T3: Stop holds the queue, drops pending wakes, and stops every delegated task beneath the thread.
        # Taken under the dispatch lock so an automatic turn cannot slip in between Stop and its latch.
        with self._dispatch_lock:
            self.store.set_hold(key, "stopped")
            self.store.execute("UPDATE recoveries SET status = 'cancelled' WHERE session_key = ? AND status = 'armed'", (key,))
            self._mark_cancel_tree(key, "parent stopped")
        threading.Thread(target=self.stop_descendants, args=(key, "parent stopped"), daemon=True,
                         name="t3o-stop").start()
        self.broadcast("session.stopped", {"session_key": key})

    def _on_user_prompt(self, params: dict, resp: Any) -> None:
        if self._paused:
            return
        sid = str(params.get("session_id") or "")
        key, _ = self._key_of_sid(sid)
        if not key or not isinstance(resp, dict) or "error" in resp:
            return
        self.store.set_hold(key, None)
        self.store.upsert_turn(key, last_user_at=_now())

    def _deliver(self, key: str, sid: str, session: dict, text: str, **kwargs: Any) -> bool:
        """Automatic turn on ``key``: refused while the thread is held (Stop) or Stop-latched."""
        return gw.deliver_turn(sid, session, text, guard=lambda: not self.store.hold(key),
                               lock=self._dispatch_lock, **kwargs)

    # ── delegation ──────────────────────────────────────────────────────────
    def resolve_parent(self, parent_agent: Any = None, session_key: str = "") -> tuple[str, dict, str]:
        sid, session = gw.session_for_agent(parent_agent) if parent_agent is not None else (None, None)
        if sid is None and session_key:
            sid, session = self.live_session(session_key)
        if sid is None or session is None:
            raise OrchestratorError("parent_not_active",
                                    "No live Hermes Desktop session owns this call. Orchestration tools run inside "
                                    "a Desktop/TUI session served by `hermes serve`.")
        key = str(getattr(session.get("agent"), "session_id", "") or session.get("session_key") or "")
        return sid, session, self.root(key, gw.profile_of(session))

    def catalog(self, profile: Optional[str]) -> list[dict]:
        res = gw.call("model.options", {"profile": profile} if profile else {})
        out = []
        for p in res.get("providers") or []:
            out.append({"provider": p.get("slug"), "name": p.get("name"), "authenticated": bool(p.get("authenticated", True)),
                        "current": bool(p.get("is_current")), "models": list(p.get("models") or [])})
        return out

    def capabilities(self, parent_agent: Any = None, for_profile: Optional[str] = None) -> dict:
        try:
            _, session, key = self.resolve_parent(parent_agent)
            agent = session.get("agent")
            profile = gw.profile_of(session)
            inherited = {"provider": getattr(agent, "provider", None), "model": getattr(agent, "model", None),
                         "session_key": key, "profile": profile or "default"}
        except OrchestratorError:
            inherited, profile = None, None
        if for_profile:
            profile, _ = self._target_profile({"profile": for_profile}, profile)
        providers = self.catalog(profile)
        return {
            "inherited": inherited,
            "profiles": self.profiles() if self.store.setting("cross_profile_delegation") else [],
            "providers": [{**p, "canDelegate": p["authenticated"] and bool(p["models"]),
                           "models": p["models"][:40], "modelCount": len(p["models"])} for p in providers],
            "features": {"delegation": True, "asyncWait": True, "cancel": True, "fork": True, "nativeForkProviders": list(NATIVE_FORK_PROVIDERS),
                         "mergeBack": True, "durableQueue": True, "steer": True, "restart": True,
                         "continueAfterRestart": self.store.setting("continue_after_restart"),
                         "resumeOnUsageLimit": self.store.setting("resume_on_usage_limit"),
                         "schedules": ["interval", "fixed_time", "webhook"]},
            "limits": {"maxDepth": self.store.setting("max_depth"),
                       "maxRunningPerParent": self.store.setting("max_running_per_parent")},
        }

    def _resolve_target(self, target: dict, session: dict, profile: Optional[str]) -> tuple[str, str]:
        agent = session.get("agent")
        provider = str(target.get("provider") or "").strip()
        model = str(target.get("model") or "").strip()
        if not provider and not model:
            return str(getattr(agent, "provider", "") or ""), str(getattr(agent, "model", "") or "")
        catalog = self.catalog(profile)
        by_slug = {p["provider"]: p for p in catalog}
        if provider:
            p = by_slug.get(provider)
            if p is None:
                raise OrchestratorError("provider_unavailable",
                                        f"Unknown provider '{provider}'. Available: {', '.join(sorted(by_slug))}")
            if not p["authenticated"]:
                raise OrchestratorError("provider_unavailable", f"Provider '{provider}' is not authenticated.")
            if not model:
                if not p["models"]:
                    raise OrchestratorError("model_unavailable", f"Provider '{provider}' advertises no models.")
                model = p["models"][0]
            elif p["models"] and model not in p["models"]:
                raise OrchestratorError("model_unavailable",
                                        f"Model '{model}' is not advertised by '{provider}'. Try: {', '.join(p['models'][:12])}")
            return provider, model
        # Model only: prefer the parent's provider, then the first authenticated provider that lists it.
        current = str(getattr(agent, "provider", "") or "")
        order = sorted(catalog, key=lambda p: (p["provider"] != current, not p["current"]))
        for p in order:
            if p["authenticated"] and model in p["models"]:
                return p["provider"], model
        raise OrchestratorError("model_unavailable", f"No authenticated provider advertises model '{model}'.")

    @staticmethod
    def _pname(profile: Optional[str]) -> str:
        return profile or "default"

    def profiles(self) -> list[dict]:
        """Every profile on this machine with its default route (for t3_capabilities and the pane)."""
        out = []
        for name, home in self._profiles():
            model = provider = ""
            with contextlib.suppress(Exception):
                import hermes_yaml
                cfg = hermes_yaml.safe_load((home / "config.yaml").read_text()) or {}
                m = cfg.get("model") if isinstance(cfg.get("model"), dict) else {}
                model, provider = str(m.get("default") or ""), str(m.get("provider") or "")
            out.append({"profile": self._pname(name), "defaultProvider": provider, "defaultModel": model})
        return out

    def _target_profile(self, target: dict, profile: Optional[str]) -> tuple[Optional[str], bool]:
        """(child profile, is cross-profile). The target profile must exist on this backend."""
        want = str(target.get("profile") or "").strip()
        if not want or self._pname(want) == self._pname(profile):
            return profile, False
        if not self.store.setting("cross_profile_delegation"):
            raise OrchestratorError("profile_unavailable", "Cross-profile delegation is turned off in orchestrator settings.")
        known = {self._pname(n): self._pname(n) for n, _ in self._profiles()}
        if want not in known:
            raise OrchestratorError("profile_unavailable",
                                    f"Unknown profile '{want}'. Available: {', '.join(sorted(known))}")
        return known[want], True

    def _child_title(self, task: dict) -> str:
        return task.get("_unique_title") or f"↳ {task['title']}"

    def unique_title(self, title: str, profile: Optional[str]) -> str:
        """Hermes titles are unique per profile: suffix #2, #3 … like its own lineage variants."""
        title = title.strip() or "Untitled"
        try:
            db = gw.open_db(profile)
        except Exception:
            return title
        try:
            candidate, n = title, 1
            while n < 200 and db.get_session_by_title(candidate):
                n += 1
                candidate = f"{title} #{n}"
            return candidate
        except Exception:
            return title
        finally:
            with contextlib.suppress(Exception):
                db.close()

    def _brief(self, task_id: str, task: str, role: str, parent_title: str, depth: int) -> str:
        return (
            f"You are a delegated child agent (T3 orchestrator task {task_id}, role: {role}) working for a parent "
            f"agent in the thread \"{parent_title or 'untitled'}\". You only know what is in this brief: the parent's "
            "conversation is not available to you. Work autonomously. No one will answer questions during the task, "
            "so make reasonable assumptions and state them.\n"
            f"{_ROLE_HINT.get(role, _ROLE_HINT['general'])}\n"
            "Your final message is returned to the parent verbatim, so end with a self-contained report: what you did, "
            "the results or findings, files changed, commands/tests run with outcomes, and open issues.\n"
            + ("You may delegate further with t3_delegate if it genuinely helps.\n" if depth < int(self.store.setting("max_depth")) else "")
            + f"\nTask:\n{task}"
        )

    def delegate(self, args: dict, parent_agent: Any = None, session_key: str = "") -> dict:
        task_text = str(args.get("task") or "").strip()
        if not task_text:
            raise OrchestratorError("invalid_request", "task is required")
        role = str(args.get("role") or "general")
        if role not in ROLES:
            raise OrchestratorError("invalid_request", f"role must be one of {', '.join(ROLES)}")
        mode = str(args.get("mode") or "async")
        if mode not in ("async", "wait"):
            raise OrchestratorError("invalid_request", "mode must be async or wait")
        parent_sid, session, parent_key = self.resolve_parent(parent_agent, session_key)
        profile = gw.profile_of(session)
        request_id = str(args.get("client_request_id") or "").strip() or None
        if request_id and (existing := self.store.task_by_request(parent_key, request_id)):
            return self._maybe_wait(existing, mode, args)
        parent_task = self.store.task_for_child(parent_key)
        depth = (parent_task["depth"] + 1) if parent_task else 1
        if depth > int(self.store.setting("max_depth")):
            raise OrchestratorError("depth_exceeded", f"Delegation depth limit ({self.store.setting('max_depth')}) reached.")
        running = [t for t in self.store.tasks_for_parent(parent_key, active_only=True)]
        if len(running) >= int(self.store.setting("max_running_per_parent")):
            raise OrchestratorError("too_many_tasks", f"{len(running)} delegated tasks are already active for this thread.")
        target = args.get("target") if isinstance(args.get("target"), dict) else {}
        child_profile, cross = self._target_profile(target, profile)
        # Another profile runs as itself: its default model, config and workspace unless overridden.
        provider, model = self._resolve_target(target, {"agent": None} if cross else session, child_profile)
        title = _clip(str(args.get("title") or "").strip() or task_text.splitlines()[0], 80)
        task_id = new_id("task")
        cwd = None if cross else (str(session.get("cwd") or "") or None)
        parent_title = ""
        with contextlib.suppress(Exception):
            row = gw.session_row(parent_key, profile)
            parent_title = (row or {}).get("title") or ""
        row = {
            "id": task_id, "parent_key": parent_key, "profile": child_profile, "parent_profile": profile,
            "root_key": (parent_task["root_key"] if parent_task else parent_key), "depth": depth,
            "title": title, "role": role, "provider": provider, "model": model, "cwd": cwd,
            "prompt": task_text, "status": "starting", "created_at": _now(), "client_request_id": request_id,
            "delivery": "none",
        }
        self.store.insert("tasks", row)
        self.store.set_hold(parent_key, None)
        try:
            child_title = self.unique_title(f"↳ {title}", child_profile)
            params: dict = {"title": child_title, "source": "desktop", "close_on_disconnect": False}
            if provider:
                params["provider"] = provider
            if model:
                params["model"] = model
            if cwd:
                params.update(cwd=cwd, cwd_explicit=True)
            params["profile"] = self._pname(child_profile)  # explicit: never the backend's launch profile
            if isinstance(target.get("reasoning_effort"), str):
                params["reasoning_effort"] = target["reasoning_effort"]
            params["idempotency_key"] = f"t3o:{task_id}"
            created = gw.call("session.create", params)
            child_sid, child_key = str(created["session_id"]), str(created["stored_session_id"])
            info = created.get("info") or {}
            if cross and not (info.get("provider") and info.get("model")):
                default = next((p for p in self.profiles() if p["profile"] == self._pname(child_profile)), {})
                info = {**info, "provider": info.get("provider") or default.get("defaultProvider"),
                        "model": info.get("model") or default.get("defaultModel")}
            self.store.update("tasks", task_id, {"child_key": child_key, "provider": info.get("provider") or provider,
                                                 "model": info.get("model") or model, "child_title": child_title})
            self.store.insert("transfers", {
                "id": new_id("xfer"), "type": "subagent_spawn", "source_key": parent_key, "target_key": child_key,
                "profile": child_profile, "strategy": "prompt_only", "status": "consumed", "delivery": "now",
                "handoff": None, "created_by": "agent", "created_at": _now(), "consumed_at": _now(),
            })
            brief = self._brief(task_id, task_text, role, parent_title, depth)
            if cross:
                brief = (f"You are running as the Hermes profile '{self._pname(child_profile)}', on behalf of the "
                         f"profile '{self._pname(profile)}'. Use this profile's own knowledge, memory and tools.\n\n" + brief)
            with self._dispatch_lock:
                # Stop / cancel may have landed while the child session was being created.
                current = self.store.task(task_id)
                if current["cancel_requested"] or current["status"] in TERMINAL:
                    with contextlib.suppress(Exception):
                        gw.call("session.close", {"session_id": child_sid})
                    self._finalize(current, {"status": "cancelled", "error": current["error"] or "cancelled before start"})
                    return self.task_view(self.store.task(task_id))
                res = gw.submit(child_sid, brief)
            # Conditional: a fast child may already have finished on the engine thread.
            self.store.update("tasks", task_id, {"status": "running", "started_at": _now()}, where="status = 'starting'")
            self.store.log("task.started", task_id, {"provider": provider, "model": model, "submit": res.get("status")})
            chatframe.announce(self, self.store.task(task_id), "subagent.start")
        except Exception as exc:
            msg = exc.args[0] if isinstance(exc, gw.GatewayError) else str(exc)
            self.store.update("tasks", task_id, {"status": "failed", "error": _clip(f"could not start child: {msg}", 2000),
                                                 "finished_at": _now(), "delivery": "skipped"})
            self._task_changed(task_id)
            raise OrchestratorError("orchestration_error", f"Could not start the child agent: {msg}") from exc
        self._task_changed(task_id)
        return self._maybe_wait(self.store.task(task_id), mode, args)

    def _maybe_wait(self, task: dict, mode: str, args: dict) -> dict:
        if mode != "wait" or task["status"] in TERMINAL:
            if mode == "wait" and task["status"] in TERMINAL:
                self.ack(task["id"])
            return self.task_view(self.store.task(task["id"]), wait_timed_out=False)
        timeout = float(args.get("timeout_s") or 600)
        ev = self.waiters.setdefault(task["id"], threading.Event())
        # Re-check after registering: the child may have finished in between.
        if self.store.task(task["id"])["status"] in TERMINAL:
            ev.set()
        done = ev.wait(max(1.0, min(timeout, 3600.0)))
        self.waiters.pop(task["id"], None)
        current = self.store.task(task["id"])
        if done and current["status"] in TERMINAL:
            self.ack(task["id"])
            current = self.store.task(task["id"])
        return self.task_view(current, wait_timed_out=not done)

    def ack(self, task_id: str) -> None:
        """Reading a terminal result acknowledges its automatic parent delivery (T3 semantics)."""
        self.store.execute("UPDATE tasks SET delivery = 'read', delivered_at = ? WHERE id = ? AND delivery = 'pending'",
                           (_now(), task_id))

    def task_view(self, t: Optional[dict], *, wait_timed_out: Optional[bool] = None) -> dict:
        if t is None:
            return {}
        progress = loads(t.get("progress"), {}) or {}
        live = self.live.get(t["id"]) or {}
        tail = live.get("tail") or progress.get("tail")
        end = t.get("finished_at") or _now()
        duration = (end - t["started_at"]) if t.get("started_at") else 0.0
        view = {
            "taskId": t["id"], "title": t["title"], "role": t["role"], "status": t["status"],
            "workState": ("result_available" if t["status"] in TERMINAL else
                          "waiting_for_limit_reset" if t["status"] == "waiting" else "working"),
            "provider": t["provider"], "model": t["model"], "parentSessionKey": t["parent_key"],
            "childSessionKey": t["child_key"], "profile": self._pname(t["profile"]),
            "parentProfile": self._pname(t.get("parent_profile") if "parent_profile" in t.keys() else t["profile"]),
            "crossProfile": self._pname(t["profile"]) != self._pname(t.get("parent_profile") or t["profile"]),
            "depth": t["depth"],
            "createdAt": t["created_at"], "startedAt": t["started_at"], "finishedAt": t["finished_at"],
            "durationSeconds": round(duration, 1),
            "progress": {"toolCount": t.get("tool_count") or 0, "lastTool": live.get("tool") or progress.get("tool"),
                         "lastToolHint": live.get("tool_hint") or progress.get("tool_hint"),
                         "todos": live.get("todos") or progress.get("todos"),
                         "tail": _clip(tail, 400) if tail else None},
            "summary": t["result"] if t["status"] in TERMINAL else None,
            "error": t["error"], "delivery": t["delivery"], "limitResetAt": t.get("limit_reset_at"),
            "cancelRequested": bool(t["cancel_requested"]),
            "pendingChildren": len(self.store.tasks_for_parent(t["child_key"], active_only=True)) if t["child_key"] else 0,
        }
        if wait_timed_out is not None:
            view["waitTimedOut"] = wait_timed_out
        return view

    def task_status(self, task_id: str, parent_key: Optional[str] = None) -> dict:
        t = self.store.task(task_id)
        if t is None or (parent_key and t["parent_key"] != parent_key and t["root_key"] != parent_key):
            raise OrchestratorError("task_not_found", f"No task {task_id} for this thread.")
        if t["status"] in TERMINAL:
            self.ack(task_id)
        return self.task_view(self.store.task(task_id))

    def cancel(self, task_id: str, reason: str = "cancelled", parent_key: Optional[str] = None) -> dict:
        t = self.store.task(task_id)
        if t is None or (parent_key and t["parent_key"] != parent_key and t["root_key"] != parent_key):
            raise OrchestratorError("task_not_found", f"No task {task_id} for this thread.")
        # Durable intent first (any process may call this); the owning backend enforces it.
        with self._dispatch_lock:
            self._mark_cancel(t, reason)
            if t["child_key"]:
                self._mark_cancel_tree(t["child_key"], reason)
        if gw.is_serve_process():
            self._enforce_tree(t)
        return self.task_view(self.store.task(task_id))

    def _mark_cancel(self, t: dict, reason: str) -> None:
        self.store.update("tasks", t["id"], {"cancel_requested": 1, "error": _clip(reason, 500)},
                          where="status NOT IN ('completed','failed','cancelled','interrupted')")
        if t["child_key"]:
            self.store.set_hold(t["child_key"], "cancelled")
            self.store.execute("UPDATE recoveries SET status = 'cancelled' WHERE session_key = ? AND status = 'armed'",
                               (t["child_key"],))

    def _mark_cancel_tree(self, key: str, reason: str, seen: Optional[set] = None) -> list[dict]:
        """Mark every descendant task — through finished intermediate children — as cancel-requested."""
        seen = seen if seen is not None else set()
        out = []
        if key in seen:
            return out
        seen.add(key)
        for t in self.store.tasks_for_parent(key):
            if t["status"] not in TERMINAL:
                self._mark_cancel(t, reason)
                out.append(t)
            if t["child_key"]:
                out.extend(self._mark_cancel_tree(t["child_key"], reason, seen))
        return out

    def _enforce_tree(self, t: dict) -> None:
        self._enforce_cancel(self.store.task(t["id"]))
        if t["child_key"]:
            self.stop_descendants(t["child_key"], "ancestor cancelled", mark=False)

    def _enforce_cancel(self, t: Optional[dict]) -> None:
        """Owner engine only: interrupt a cancel-requested task's child, or finalize it if idle."""
        if not t or t["status"] in TERMINAL or not t["cancel_requested"]:
            return
        self._enforced[t["id"]] = _now()
        sid, s = self.live_session(t["child_key"] or "", t["profile"])
        if sid and gw.is_running(s):
            with contextlib.suppress(Exception):
                gw.interrupt(sid)
            # The interrupted terminal frame finalizes the task; fall back if it never arrives.
            threading.Timer(20.0, self._force_cancel, args=(t["id"], t["error"] or "cancelled")).start()
        else:
            self._force_cancel(t["id"], t["error"] or "cancelled")

    def _force_cancel(self, task_id: str, reason: str) -> None:
        t = self.store.task(task_id)
        if t and t["status"] not in TERMINAL:
            self._finalize(t, {"status": "cancelled", "error": reason, "result": self._last_reply(t) or None})

    def stop_descendants(self, key: str, reason: str, *, mark: bool = True) -> int:
        """Stop every delegated task beneath ``key``, including grandchildren of finished children."""
        with self._dispatch_lock:
            if mark:
                self._mark_cancel_tree(key, reason)
            targets: list[dict] = []
            stack, seen = [key], set()
            while stack:
                k = stack.pop()
                if k in seen:
                    continue
                seen.add(k)
                for t in self.store.tasks_for_parent(k):
                    if t["status"] not in TERMINAL and t["cancel_requested"]:
                        targets.append(t)
                    if t["child_key"]:
                        stack.append(t["child_key"])
        if not gw.is_serve_process():
            return len(targets)
        for t in targets:
            with contextlib.suppress(Exception):
                self._enforce_cancel(self.store.task(t["id"]))
        return len(targets)

    def _process_cancels(self) -> None:
        """Enforce cancellations requested from other processes (e.g. /t3 cancel on a phone)."""
        for t in self.store.rows("SELECT * FROM tasks WHERE cancel_requested = 1 AND status NOT IN "
                                 "('completed','failed','cancelled','interrupted')"):
            if _now() - self._enforced.get(t["id"], 0) > 30:
                self._enforce_cancel(t)

    # ── fork & merge back ───────────────────────────────────────────────────
    def runs(self, key: str, profile: Optional[str]) -> list[dict]:
        return handoff.runs(gw.history(key, profile))

    def _source_route(self, key: str, profile: Optional[str]) -> tuple[str, str, dict]:
        row = gw.session_row(key, profile) or {}
        cfg = loads(row.get("model_config"), {}) or {}
        provider = str(row.get("billing_provider") or cfg.get("provider") or "")
        model = str(row.get("model") or cfg.get("model") or "")
        sid, s = self.live_session(key)
        agent = (s or {}).get("agent")
        if agent is not None:
            provider = str(getattr(agent, "provider", "") or provider)
            model = str(getattr(agent, "model", "") or model)
        native = cfg.get("native_agent_runtime") if isinstance(cfg.get("native_agent_runtime"), dict) else {}
        return provider, model, native

    def fork(self, source_key: str, profile: Optional[str], *, run: Optional[int] = None, title: str = "",
             target: Optional[dict] = None, created_by: str = "user") -> dict:
        target = target or {}
        msgs = gw.history(source_key, profile)
        if not msgs:
            raise OrchestratorError("invalid_request", "Nothing to fork: the thread has no messages yet.")
        all_runs = handoff.runs(msgs)
        cut = handoff.cut_for_run(msgs, run)
        point = {"run": run or len(all_runs), "messages": cut, "row_id": handoff.row_id(msgs[cut - 1]) if cut else None,
                 "runStatus": all_runs[(run or len(all_runs)) - 1]["status"] if all_runs else None}
        src_provider, src_model, native = self._source_route(source_key, profile)
        tgt_provider = str(target.get("provider") or src_provider)
        tgt_model = str(target.get("model") or (src_model if tgt_provider == src_provider else ""))
        if tgt_provider != src_provider or (target.get("model") and tgt_model != src_model):
            # Validate the target against the live catalog before anything is created.
            tgt_provider, tgt_model = self._resolve_target({"provider": tgt_provider, "model": tgt_model},
                                                           {"agent": None}, profile)
        src_title = (gw.session_row(source_key, profile) or {}).get("title") or "thread"
        name = self.unique_title(title or f"{src_title} (fork{' @ run ' + str(run) if run else ''})", profile)
        latest = run is None or run == len(all_runs)
        target_native = self._is_native_provider(tgt_provider)
        visible = [m for m in msgs[:cut] if m.get("role") in ("user", "assistant") and handoff._text(m.get("content")).strip()]
        transfer_id = new_id("xfer")

        if native.get("session_id") and tgt_provider == src_provider and latest and tgt_provider in NATIVE_FORK_PROVIDERS:
            strategy = "native_provider_fork"
            new_key, new_sid = self._create_seeded(visible, name, source_key, profile, tgt_provider, tgt_model,
                                                   cwd=native.get("cwd"))
            fork_at = self._native_fork_point(native) if tgt_provider in ("claude-cli", "claude-cli-2") else None
            point["nativeForkAt"] = fork_at
            db = gw.open_db(profile, write=True)
            try:
                db.patch_session_model_config(new_key, {"native_agent_runtime": {
                    "fork_from": native["session_id"], "fork_at": fork_at,
                    "provider": native.get("provider") or tgt_provider,
                    "runtime": native.get("runtime"), "owner": new_key, "cwd": native.get("cwd"),
                    "config_directory": native.get("config_directory", ""), "version": 1}})
            finally:
                with contextlib.suppress(Exception):
                    db.close()
            pending, text = False, None
        elif target_native:
            # Native targets cannot adopt a copied transcript: carry a portable handoff instead.
            strategy = "portable_context"
            new_key, new_sid = self._create_empty(name, profile, tgt_provider, tgt_model,
                                                  cwd=native.get("cwd") or self._session_cwd(source_key))
            text = handoff.portable_context(msgs[:cut], title=src_title, source_key=source_key,
                                            source_provider=src_provider, source_model=src_model, reason="fork",
                                            budget=int(self.store.setting("handoff_bytes")))
            pending = True
        elif native.get("session_id"):
            # A native thread (earlier point, or onto a Hermes-loop provider): Hermes owns the display mirror,
            # so the fork starts from that transcript on the target provider.
            strategy = "hermes_transcript_seeded"
            new_key, new_sid = self._create_seeded(visible, name, source_key, profile, tgt_provider, tgt_model,
                                                   cwd=native.get("cwd"))
            pending, text = False, None
        else:
            strategy = "hermes_transcript"
            sid, _ = self.ensure_live(source_key, profile, build=False)
            res = gw.call("session.branch", {"session_id": sid, "count": len(visible), "name": name,
                                             "idempotency_key": f"t3o:{transfer_id}"})
            new_key, new_sid = str(res["stored_session_id"]), str(res["session_id"])
            # session.branch builds on the profile default; pin the resolved route explicitly.
            if tgt_model:
                self._switch_model(new_sid, tgt_provider, tgt_model)
            pending, text = False, None
        point["copiedVisible"] = 0 if strategy == "portable_context" else len(visible)
        self.store.insert("transfers", {
            "id": transfer_id, "type": "fork", "source_key": source_key, "target_key": new_key, "profile": profile,
            "source_point": dumps(point), "source_provider": src_provider, "target_provider": tgt_provider,
            "strategy": strategy, "status": "pending" if pending else "consumed",
            "delivery": "next_message", "handoff": text, "created_by": created_by, "created_at": _now(),
            "consumed_at": None if pending else _now(),
        })
        if pending:
            self._deliver_transfer_now_if_native(transfer_id)
        self.broadcast("lineage.updated", {"session_key": source_key, "fork_key": new_key})
        return {"ok": True, "forkSessionKey": new_key, "forkRuntimeId": new_sid, "title": name,
                "strategy": strategy, "sourcePoint": point, "provider": tgt_provider, "model": tgt_model,
                "transferId": transfer_id}

    def _native_fork_point(self, native: dict) -> Optional[str]:
        """UUID of the source's last native transcript entry, so the fork is pinned to this moment."""
        if native.get("config_directory"):
            return None  # transcripts live elsewhere; fall back to forking the latest state
        with contextlib.suppress(Exception):
            from claude_agent_sdk import get_session_messages
            msgs = get_session_messages(native["session_id"], directory=native.get("cwd") or None)
            if msgs:
                return getattr(msgs[-1], "uuid", None)
        return None

    def _session_cwd(self, key: str) -> Optional[str]:
        sid, s = self.live_session(key)
        return str((s or {}).get("cwd") or "") or None

    def _is_native_provider(self, provider: str) -> bool:
        with contextlib.suppress(Exception):
            from agent.native_runtime import runtime_profile
            return bool(runtime_profile(provider))
        return False

    def _create_seeded(self, visible: list[dict], title: str, parent_key: str, profile: Optional[str],
                       provider: str, model: str, cwd: Optional[str]) -> tuple[str, str]:
        seed = [{"role": m["role"], "content": handoff._text(m.get("content"))} for m in visible]
        params: dict = {"title": title, "messages": seed, "parent_session_id": parent_key, "source": "desktop"}
        params.update({k: v for k, v in (("provider", provider), ("model", model), ("profile", profile)) if v})
        if cwd:
            params.update(cwd=cwd, cwd_explicit=True)
        res = gw.call("session.create", params)
        return str(res["stored_session_id"]), str(res["session_id"])

    def _create_empty(self, title: str, profile: Optional[str], provider: str, model: str,
                      cwd: Optional[str]) -> tuple[str, str]:
        params: dict = {"title": title, "source": "desktop"}
        params.update({k: v for k, v in (("provider", provider), ("model", model), ("profile", profile)) if v})
        if cwd:
            params.update(cwd=cwd, cwd_explicit=True)
        res = gw.call("session.create", params)
        return str(res["stored_session_id"]), str(res["session_id"])

    def _switch_model(self, sid: str, provider: str, model: str) -> None:
        value = " ".join(x for x in (model, f"--provider {provider}" if provider else "", "--session") if x)
        gw.call("config.set", {"session_id": sid, "key": "model", "value": value})

    def merge_back(self, fork_key: str, profile: Optional[str], *, target_key: Optional[str] = None,
                   run: Optional[int] = None, delivery: str = "auto", created_by: str = "user") -> dict:
        origin = self.store.fork_origin(fork_key)
        target_key = target_key or (origin["source_key"] if origin else None)
        if not target_key:
            raise OrchestratorError("invalid_request", "This thread is not a fork; pass target_key explicitly.")
        msgs = gw.history(fork_key, profile)
        cut = handoff.cut_for_run(msgs, run)
        base = 0
        if origin and origin["target_key"] == fork_key and origin["source_key"] == target_key:
            point = loads(origin.get("source_point"), {}) or {}
            n_visible = point.get("copiedVisible")
            if n_visible is None:  # forks recorded before copiedVisible existed
                prefix = int(point.get("messages") or 0) if origin["strategy"] != "portable_context" else 0
                src = gw.history(target_key, profile)[:prefix]
                n_visible = sum(1 for m in src if m.get("role") in ("user", "assistant") and handoff._text(m.get("content")).strip())
            # The fork's transcript starts with exactly that many copied visible rows: skip them.
            seen = 0
            base = len(msgs) if n_visible else 0
            if n_visible:
                for i, m in enumerate(msgs):
                    if m.get("role") in ("user", "assistant") and handoff._text(m.get("content")).strip():
                        seen += 1
                        if seen == n_visible:
                            base = i + 1
                            break
        fork_title = (gw.session_row(fork_key, profile) or {}).get("title") or "fork"
        text = handoff.delta_context(msgs[:cut], fork_title=fork_title, fork_key=fork_key, base_count=base,
                                     budget=int(self.store.setting("handoff_bytes")))
        tgt_provider, _, native = self._source_route(target_key, profile)
        if delivery == "auto":
            delivery = "now" if (native.get("session_id") or self._is_native_provider(tgt_provider)) else "next_message"
        transfer_id = new_id("xfer")
        self.store.insert("transfers", {
            "id": transfer_id, "type": "merge_back", "source_key": fork_key, "target_key": target_key, "profile": profile,
            "source_point": dumps({"run": run, "messages": cut}), "base_point": dumps({"messages": base}),
            "strategy": "delta_context", "status": "pending", "delivery": delivery, "handoff": text,
            "created_by": created_by, "created_at": _now(),
        })
        self.broadcast("lineage.updated", {"session_key": target_key, "merge_from": fork_key})
        return {"ok": True, "transferId": transfer_id, "targetSessionKey": target_key, "delivery": delivery,
                "deltaMessages": max(0, cut - base), "handoffBytes": len(text.encode("utf-8")),
                "note": ("Delivered with the next message sent in the target thread." if delivery == "next_message"
                         else "Delivered as a context turn as soon as the target thread is idle.")}

    def _deliver_transfer_now_if_native(self, transfer_id: str) -> None:
        x = self.store.transfer(transfer_id)
        if x and x["status"] == "pending":
            self.store.execute("UPDATE transfers SET delivery = 'now' WHERE id = ?", (transfer_id,))

    def consume_for_prompt(self, session_key: str) -> Optional[str]:
        """pre_llm_call: pending next-message transfers for this session become turn context."""
        session_key = self.root(session_key, gw.profile_of(gw.live_for_key(session_key)[1]))
        pending = [x for x in self.store.pending_transfers_for(session_key) if x["delivery"] == "next_message"]
        if not pending:
            return None
        texts = []
        for x in pending:
            if self.store.execute("UPDATE transfers SET status = 'consumed', consumed_at = ? WHERE id = ? AND status = 'pending'",
                                  (_now(), x["id"])).rowcount:
                texts.append(x["handoff"] or "")
                self.store.log("transfer.consumed", x["id"], x["type"])
        if texts:
            self.broadcast("lineage.updated", {"session_key": session_key})
        return "\n\n".join(t for t in texts if t) or None

    # ── queue / steer ───────────────────────────────────────────────────────
    def queue_list(self, key: str) -> dict:
        items = self.store.queue(key)
        hold = self.store.hold(key)
        return {"sessionKey": key, "held": bool(hold), "holdReason": (hold or {}).get("reason"),
                "items": [{"id": q["id"], "text": q["text"], "position": i + 1, "status": q["status"],
                           "createdAt": q["created_at"], "updatedAt": q["updated_at"], "createdBy": q["created_by"]}
                          for i, q in enumerate(items)]}

    def queue_add(self, key: str, text: str, profile: Optional[str], *, created_by: str = "user") -> dict:
        text = str(text or "").strip()
        if not text:
            raise OrchestratorError("invalid_request", "text is required")
        last = self.store.row("SELECT MAX(position) AS p FROM queue WHERE session_key = ?", (key,))
        pos = float((last or {}).get("p") or 0) + 1.0
        qid = new_id("q")
        self.store.insert("queue", {"id": qid, "session_key": key, "profile": profile, "position": pos, "text": text,
                                    "status": "pending", "created_by": created_by, "created_at": _now(), "updated_at": _now()})
        self._queue_changed(key)
        return {"ok": True, "queuedId": qid, **self.queue_list(key)}

    def _queue_item(self, key: str, qid: str) -> dict:
        q = self.store.row("SELECT * FROM queue WHERE id = ? AND session_key = ?", (qid, key))
        if q is None:
            raise OrchestratorError("not_found", f"No queued message {qid} in this thread.")
        if q["status"] != "pending":
            raise OrchestratorError("not_queued", f"Queued message {qid} is already {q['status']}.")
        return q

    def queue_edit(self, key: str, qid: str, text: str) -> dict:
        self._queue_item(key, qid)
        text = str(text or "").strip()
        if not text:
            raise OrchestratorError("invalid_request", "text is required")
        self.store.update("queue", qid, {"text": text, "updated_at": _now()}, where="status = 'pending'")
        self._queue_changed(key)
        return {"ok": True, **self.queue_list(key)}

    def queue_reorder(self, key: str, qid: str, before_id: Optional[str]) -> dict:
        self._queue_item(key, qid)
        with self.store.transaction():
            items = self.store.rows("SELECT id FROM queue WHERE session_key = ? AND status = 'pending' AND id != ? "
                                    "ORDER BY position, created_at", (key, qid))
            ids = [q["id"] for q in items]
            idx = ids.index(before_id) if before_id in ids else len(ids)
            ids.insert(idx, qid)
            for i, item_id in enumerate(ids):
                self.store.execute("UPDATE queue SET position = ?, updated_at = ? WHERE id = ? AND status = 'pending'",
                                   (float(i + 1), _now(), item_id))
        self._queue_changed(key)
        return {"ok": True, **self.queue_list(key)}

    def queue_remove(self, key: str, qid: str) -> dict:
        self._queue_item(key, qid)
        self.store.update("queue", qid, {"status": "cancelled", "updated_at": _now()})
        self._queue_changed(key)
        return {"ok": True, **self.queue_list(key)}

    def queue_clear(self, key: str) -> dict:
        self.store.execute("UPDATE queue SET status = 'cancelled', updated_at = ? WHERE session_key = ? AND status = 'pending'",
                           (_now(), key))
        self._queue_changed(key)
        return {"ok": True, **self.queue_list(key)}

    def queue_hold(self, key: str, held: bool) -> dict:
        self.store.set_hold(key, "held by user" if held else None)
        if not held:
            # An explicit resume also lifts core's post-Stop latch, which otherwise waits for the next
            # typed message before any automatic turn may run.
            sid, s = self.live_session(key)
            if s is not None and s.get("_turn_cancel_requested") and not s.get("running"):
                with s["history_lock"]:
                    s["_turn_cancel_requested"] = False
        self._queue_changed(key)
        return {"ok": True, **self.queue_list(key)}

    def queue_promote(self, key: str, qid: str, how: str = "steer") -> dict:
        q = self._queue_item(key, qid)
        sid, s = self.live_session(key)
        if sid is None or not gw.is_running(s):
            raise OrchestratorError("no_active_run", "The thread has no active turn to steer; it will run from the queue.")
        status = self._steer(sid, s, q["text"], how)
        if status == "rejected":
            raise OrchestratorError("steer_rejected", "The active turn rejected the steer.")
        self.store.update("queue", qid, {"status": "sent", "sent_at": _now(), "updated_at": _now()})
        self._queue_changed(key)
        return {"ok": True, "steerStatus": status, **self.queue_list(key)}

    def _steer(self, sid: str, session: Optional[dict], text: str, how: str) -> str:
        """In-turn steer for Hermes-loop sessions; native runtimes never read Hermes's steer buffer,
        so a steer there becomes interrupt-and-restart (T3's policy for providers without steering)."""
        provider = str(getattr((session or {}).get("agent"), "provider", "") or (session or {}).get("provider") or "")
        if how == "steer" and not self._is_native_provider(provider):
            return gw.steer(sid, text)
        return gw.redirect(sid, text)

    def _queue_changed(self, key: str) -> None:
        self.broadcast("queue.updated", {"session_key": key})

    def send(self, key: str, profile: Optional[str], text: str, mode: str = "auto") -> dict:
        """T3's t3_thread_send: auto starts idle / steers active; queue, steer and restart are explicit."""
        text = str(text or "").strip()
        if not text:
            raise OrchestratorError("invalid_request", "message is required")
        if mode == "queue":
            return self.queue_add(key, text, profile, created_by="agent")
        sid, s = self.live_session(key)
        running = sid is not None and gw.is_running(s)
        if mode in ("steer", "restart"):
            if not running:
                raise OrchestratorError("no_active_run", f"mode={mode} needs an active turn in the target thread.")
            status = self._steer(sid, s, text, mode)
            return {"ok": status != "rejected", "status": status}
        if mode != "auto":
            raise OrchestratorError("invalid_request", "mode must be auto, queue, steer or restart")
        if running:
            status = self._steer(sid, s, text, "steer")
            if status != "rejected":
                return {"ok": True, "status": f"steered ({status})"}
            return self.queue_add(key, text, profile, created_by="agent")
        sid, s = self.ensure_live(key, profile)
        res = gw.submit(sid, text)
        return {"ok": True, "status": res.get("status") or "started"}

    # ── recovery ────────────────────────────────────────────────────────────
    def _arm_usage_limit(self, key: str, profile: Optional[str], reset_at: float, surface: dict) -> bool:
        if reset_at <= _now() - 60 or reset_at > _now() + 8 * 86400:
            return False
        detail = dumps({k: surface.get(k) for k in ("provider", "model", "code", "layer")})
        existing = self.store.row("SELECT * FROM recoveries WHERE session_key = ? AND kind = 'usage_limit' AND reset_at = ?",
                                  (key, reset_at))
        if existing is None:
            self.store.insert("recoveries", {"id": new_id("rec"), "session_key": key, "profile": profile,
                                             "kind": "usage_limit", "reset_at": reset_at, "status": "armed",
                                             "detail": detail, "created_at": _now()})
        elif existing["status"] != "armed":
            # The same window failed again after a resume fired: arm it again, a bounded number of times.
            if int(existing.get("attempts") or 0) >= 3:
                self.store.update("recoveries", existing["id"], {"status": "exhausted"})
                return False
            self.store.update("recoveries", existing["id"], {"status": "armed", "created_at": _now(), "fired_at": None,
                                                             "detail": detail})
        self.store.log("recovery.armed", key, {"reset_at": reset_at})
        self.broadcast("recovery.updated", {"session_key": key})
        return True

    def recoveries(self, key: Optional[str] = None) -> list[dict]:
        if key:
            return self.store.rows("SELECT * FROM recoveries WHERE session_key = ? ORDER BY created_at DESC LIMIT 20", (key,))
        return self.store.rows("SELECT * FROM recoveries WHERE status = 'armed' OR created_at > ? ORDER BY created_at DESC",
                               (_now() - 86400,))

    def cancel_recovery(self, rec_id: str) -> None:
        self.store.update("recoveries", rec_id, {"status": "cancelled"}, where="status = 'armed'")
        self.broadcast("recovery.updated", {})

    def _profiles(self) -> list[tuple[Optional[str], Path]]:
        root = gw.profile_home(None)
        out: list[tuple[Optional[str], Path]] = [(None, root)]
        pdir = root / "profiles"
        if pdir.is_dir():
            for p in sorted(pdir.iterdir()):
                if p.is_dir() and (p / "config.yaml").exists():
                    out.append((p.name, p))
        return out

    def _boot(self) -> None:
        """Once per engine start: requeue in-flight sends, recover tasks and turns a restart cut."""
        # A send interrupted by the restart: if the transcript already holds it, it was admitted.
        for q in self.store.rows("SELECT * FROM queue WHERE status = 'sending'"):
            sent = False
            with contextlib.suppress(Exception):
                users = [handoff._text(m.get("content")) for m in gw.history(self.tip(q["session_key"], q["profile"]),
                                                                              q["profile"]) if m.get("role") == "user"]
                sent = q["text"] in users[-5:]
            self.store.update("queue", q["id"], {"status": "sent" if sent else "pending", "updated_at": _now()})
        if not self.store.setting("continue_after_restart"):
            for t in self.store.active_tasks():
                if t["status"] in ("starting", "running") and t["started_at"] and t["started_at"] < self._started_at:
                    self._finalize(t, {"status": "interrupted", "error": "The Hermes backend restarted before the task finished "
                                                                          "(continue-after-restart is off)."})
            self.store.execute("UPDATE recoveries SET status = 'disabled' WHERE kind = 'restart' AND status = 'armed'")
            return
        self._collect_cut_turns(markers=True)
        # Children with restart intent are continued (or settled) by _fire_restarts.
        covered = {r["session_key"] for r in self.store.rows(
            "SELECT session_key FROM recoveries WHERE kind = 'restart' AND status = 'armed'")}
        for t in self.store.active_tasks():
            if t["status"] not in ("starting", "running") or not t["child_key"] or t["child_key"] in covered:
                continue
            if t["started_at"] and t["started_at"] >= self._started_at:
                continue
            # A task whose child left no marker was between turns; continue it explicitly.
            try:
                sid, s = self.ensure_live(t["child_key"], t["profile"])
                if not gw.is_running(s) and not self._deliver(t["child_key"], sid, s, CONTINUE_PROMPT, display_kind="auto_continue"):
                    raise RuntimeError("child busy")
            except Exception as exc:
                self._finalize(t, {"status": "interrupted", "error": f"The Hermes backend restarted and the task could not "
                                                                     f"be continued: {exc}"})

    # ── restart continuation (T3's "Continue threads after restarts") ───────
    # Intent is a ``recoveries`` row (kind 'restart', reset_at = when the turn was cut) whose detail
    # carries the cut turn: running or settled-with-awaited-work, prompt, runtime, background work,
    # and whether the user had stopped it or owed it an answer. Recorded by prepare_for_shutdown
    # (update / quit) or from what a dead process left behind (crash); delivered by _fire_restarts.
    def _me(self) -> tuple[int, Optional[float]]:
        if self._identity is None:
            self._identity = restart.own_identity()
        return self._identity

    def _background_work(self, key: str, session: dict) -> list[dict]:
        """Work a restart kills that the turn may be waiting on: native subagents and background
        processes. Orchestrator tasks are not listed: their own threads continue and report back."""
        work: list[dict] = []
        for n in list(chatframe.NATIVE.values()):
            if n.get("parent_key") == key and n.get("status") in ("queued", "running"):
                work.append({"kind": "subagent", "label": restart.compact_label(n.get("goal")) or "subagent", "id": n["id"]})
        registry = getattr(sys.modules.get("tools.process_registry"), "process_registry", None)
        ids = {str(session.get("session_key") or ""), gw._agent_key(session)} - {""}
        if registry is not None and ids:
            with contextlib.suppress(Exception):
                with registry._lock:
                    procs = list(registry._running.values())
                for p in procs:
                    if p.exited or not ({p.session_key, p.task_id, getattr(p, "owner_task_id", "")} & ids):
                        continue
                    awaited = bool(p.notify_on_complete or p.watch_patterns)
                    work.append({"kind": "task" if awaited else "shell", "id": p.id, "pid": p.pid,
                                 "label": restart.compact_label(p.command) or "background command"})
        return work

    def _live_entries(self) -> dict[str, dict]:
        """This process's running turns, and idle threads waiting on background work, by lineage root."""
        out: dict[str, dict] = {}
        registry = getattr(sys.modules.get("tools.process_registry"), "process_registry", None)
        background = bool(chatframe.NATIVE) or bool(getattr(registry, "_running", None))
        for sid, s in gw.sessions_snapshot():
            if s.get("_finalized") or s.get("_closing"):
                continue
            running = gw.is_running(s)
            if not running and not background:
                continue
            key, profile = self._key_of_sid(sid)
            if not key:
                continue
            work = self._background_work(key, s)
            if not running and not any(restart.is_awaited(w) for w in work):
                continue
            inflight = s.get("inflight_turn") if isinstance(s.get("inflight_turn"), dict) else {}
            provider = str(getattr(s.get("agent"), "provider", "") or s.get("provider") or "")
            out[key] = {"profile": profile, "running": running, "prompt": _clip(inflight.get("user") or "", 4000),
                        "provider": provider, "native": self._is_native_provider(provider),
                        "turn_started_at": (self.store.turn(key) or {}).get("started_at"),
                        "stopping": gw.stop_latched(s), "awaiting_user": gw.awaiting_user(sid, s), "work": work}
        return out

    def _record_live_work(self) -> None:
        """Keep this process's ``live_work`` rows current; rows a dead process leaves are a crash."""
        if self._paused or self._shutting_down or not gw.is_serve_process():
            return
        pid, start = self._me()
        entries = self._live_entries() if self.store.setting("continue_after_restart") else {}
        for key, d in entries.items():
            sig = dumps([d["running"], d["prompt"], d["stopping"], d["awaiting_user"], [w["id"] for w in d["work"]]])
            if self._live_written.get(key) == sig:
                continue
            self.store.execute(
                "INSERT INTO live_work (session_key, profile, pid, pid_start, detail, updated_at) VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(session_key) DO UPDATE SET profile = excluded.profile, pid = excluded.pid, "
                "pid_start = excluded.pid_start, detail = excluded.detail, updated_at = excluded.updated_at",
                (key, d["profile"], pid, start, dumps(d), _now()))
            self._live_written[key] = sig
        for key in [k for k in self._live_written if k not in entries]:
            self.store.execute("DELETE FROM live_work WHERE session_key = ? AND pid = ?", (key, pid))
            self._live_written.pop(key, None)

    def prepare_for_shutdown(self) -> int:
        """T3 prepareForShutdown: record intent for every live turn while it is still live. Runs from
        core's exit path right before it interrupts the turns (signal handler or atexit, once)."""
        if self._shutting_down:
            return 0
        self._shutting_down = True
        if self._paused or not gw.is_serve_process() or not self.store.setting("continue_after_restart"):
            return 0
        entries = self._live_entries()
        for key, d in entries.items():
            # One failing thread must not cost the threads after it their continuation.
            with contextlib.suppress(Exception):
                self._record_intent(key, d["profile"], {**d, "cause": "shutdown"}, _now())
        self.store.execute("DELETE FROM live_work WHERE pid = ?", (self._me()[0],))
        self.store.log("restart.prepared", "", {"threads": len(entries)})
        return len(entries)

    def _record_intent(self, key: str, profile: Optional[str], detail: dict, cut_at: float) -> None:
        existing = self.store.row("SELECT * FROM recoveries WHERE session_key = ? AND kind = 'restart' AND status = 'armed'",
                                  (key,))
        if existing is not None:
            prior = loads(existing["detail"], {}) or {}
            merged = {**detail, **prior, "work": restart.merge_work(prior.get("work") or [], detail.get("work") or []),
                      "marker": bool(prior.get("marker") or detail.get("marker"))}
            for k in ("marker_key", "marker_attempts"):
                if detail.get(k) is not None:
                    merged[k] = detail[k]
            self.store.update("recoveries", existing["id"], {"detail": dumps(merged)})
            return
        self.store.execute("INSERT OR IGNORE INTO recoveries (id, session_key, profile, kind, reset_at, status, detail, "
                           "created_at) VALUES (?, ?, ?, 'restart', ?, 'armed', ?, ?)",
                           (new_id("rec"), key, profile, cut_at, dumps(detail), _now()))
        self.store.log("restart.recorded", key, {"cause": detail.get("cause"), "running": detail.get("running")})

    def _collect_cut_turns(self, *, markers: bool = False) -> None:
        """Turn what dead processes left behind into restart intent: their ``live_work`` rows and,
        at boot, core's crash markers (a turn started too recently for the snapshot)."""
        me = self._me()
        for r in self.store.rows("SELECT * FROM live_work"):
            if (r["pid"], r["pid_start"]) == me:
                continue
            same_pid_old = r["pid"] == me[0] and r["pid_start"] != me[1]
            if not same_pid_old and not restart.writer_dead(r["pid"], r["pid_start"]):
                continue
            d = loads(r["detail"], {}) or {}
            with contextlib.suppress(Exception):
                self._record_intent(r["session_key"], r["profile"], {**d, "cause": "crash"}, float(r["updated_at"]))
            self.store.execute("DELETE FROM live_work WHERE session_key = ? AND pid = ?", (r["session_key"], r["pid"]))
        if not markers:
            return
        try:
            from tui_gateway.turn_marker import _load, _marker_path, marker_writer_state
        except Exception:
            return
        for profile, home in self._profiles():
            try:
                entries = _load(_marker_path(home))
            except Exception:
                continue
            for mkey, entry in list(entries.items()):
                if not isinstance(entry, dict) or marker_writer_state(entry) == "alive":
                    continue
                if not entry.get("auto_continue", True):
                    continue  # the mailbox owns recovery for imported turns
                started = float(entry.get("started_at") or 0)
                if _now() - started > restart.MAX_INTENT_AGE_S:
                    continue
                with contextlib.suppress(Exception):
                    key = self.root(mkey, profile)
                    provider, _, native = self._source_route(key, profile)
                    self._record_intent(key, profile, {
                        "cause": "crash", "running": True, "prompt": _clip(entry.get("prompt") or "", 4000),
                        "provider": provider, "native": bool(native.get("session_id")) or self._is_native_provider(provider),
                        "turn_started_at": started, "work": [], "marker": True, "marker_key": mkey,
                        "marker_attempts": int(entry.get("attempts") or 0), "profile": profile}, started)

    def _restart_chain(self, key: str) -> int:
        """Consecutive automatic continuations since the last turn a person (or the queue) started."""
        since = (self.store.turn(key) or {}).get("last_user_at") or 0
        r = self.store.row("SELECT COUNT(*) AS n FROM recoveries WHERE session_key = ? AND kind = 'restart' "
                           "AND status = 'fired' AND fired_at > ?", (key, since))
        return int((r or {}).get("n") or 0)

    def _fire_restarts(self) -> None:
        for r in self.store.rows("SELECT * FROM recoveries WHERE kind = 'restart' AND status = 'armed' ORDER BY created_at"):
            try:
                status = self._continue_restarted(r)
            except Exception:
                logger.info("t3o: restart continuation failed for %s", r["session_key"], exc_info=True)
                status = None
            if status is None:
                if _now() - r["created_at"] <= restart.MAX_INTENT_AGE_S:
                    continue  # not reachable or busy yet: the next tick tries again
                status = "expired"
            self._settle_restart(r, status)

    def _continue_restarted(self, r: dict) -> Optional[str]:
        """T3 continueRestartedRun. Returns the outcome, or None to retry on a later tick."""
        if not self.store.setting("continue_after_restart"):
            return "disabled"
        key, profile = r["session_key"], r["profile"]
        d = loads(r["detail"], {}) or {}
        cut_at = float(r["reset_at"] or r["created_at"])
        turn = self.store.turn(key) or {}
        # A user submission after the restart takes precedence over an automatic prompt.
        if (turn.get("last_user_at") or 0) > cut_at:
            return "superseded"
        # The user asked this turn to stop before the restart cut it.
        if self.store.hold(key) or d.get("stopping"):
            return "stopped"
        # A crash snapshot lags up to one tick: a turn that concluded in that window is done.
        if (d.get("cause") == "crash" and d.get("running") and not d.get("marker") and turn.get("status") != "running"
                and (turn.get("ended_at") or 0) >= (d.get("turn_started_at") or float("inf"))):
            return "finished"
        row: dict = {}
        with contextlib.suppress(Exception):
            row = gw.session_row(key, profile) or {}
        why = restart.blocked(lifecycle=restart.lifecycle_record(self._pname(profile), key),
                              archived=bool(row.get("archived")), awaiting_user=bool(d.get("awaiting_user")))
        if why:
            return why
        if restart.is_maintenance_command(d.get("prompt")):
            return "maintenance"
        # A turn that keeps killing the backend would otherwise restart it forever.
        if self._restart_chain(key) >= restart.MAX_CONTINUATION_CHAIN:
            logger.warning("t3o: not continuing %s, restarted %d times in a row", key, restart.MAX_CONTINUATION_CHAIN)
            return "exhausted"
        # Background processes that outlived the backend (their own process group) still report back.
        work = [w for w in d.get("work") or [] if not (w.get("pid") and restart.pid_alive(w["pid"]))]
        settled = not d.get("running")
        if settled and not any(restart.is_awaited(w) for w in work):
            if work:
                self._restart_note_next_message(key, profile, work)
            return "nothing"
        native = bool(d.get("native"))
        if d.get("marker") and not native and not work:
            # Hermes-loop crash: session.resume runs core's own bounded auto-continue (its note carries the prompt).
            sid, s = self.ensure_live(key, profile)
            if s.get("_auto_continue_scheduled") or gw.is_running(s):
                return "fired"
            srv = gw.server()
            with contextlib.suppress(Exception):
                if int(d.get("marker_attempts") or 0) >= int(srv._auto_continue_config()[2]):
                    return "exhausted"
        if d.get("marker_key"):
            # Ours to continue: core must not replay it too when the session is opened.
            with contextlib.suppress(Exception):
                from tui_gateway.turn_marker import clear_turn_marker
                clear_turn_marker(gw.profile_home(profile), d["marker_key"])
        sid, s = self._idle_session(key, profile)
        if sid is None:
            return None
        text = restart.continuation_text(work, settled)
        if d.get("cause") == "crash" and not native and not settled and d.get("prompt"):
            # A hard crash may have persisted nothing of the turn; carry the request like core does.
            srv = gw.server()
            note = None
            with contextlib.suppress(Exception):
                note = srv._auto_continue_note(d["prompt"])
            text = (f"{restart.background_note(work)}\n\n{note}" if work else note) if note else text
        return "fired" if self._deliver(key, sid, s, text, display_kind="auto_continue") else None

    def _restart_note_next_message(self, key: str, profile: Optional[str], work: list[dict]) -> None:
        """Shells nobody waits on: tell the agent with the next message instead of waking the thread."""
        self.store.insert("transfers", {
            "id": new_id("xfer"), "type": "restart_note", "source_key": key, "target_key": key, "profile": profile,
            "strategy": "note", "status": "pending", "delivery": "next_message", "handoff": restart.background_note(work),
            "created_by": "system", "created_at": _now()})

    def _settle_restart(self, r: dict, status: str) -> None:
        values: dict = {"status": status}
        if status == "fired":
            values.update(fired_at=_now(), attempts=int(r.get("attempts") or 0) + 1)
        if not self.store.update("recoveries", r["id"], values, where="status = 'armed'"):
            return
        key = r["session_key"]
        d = loads(r["detail"], {}) or {}
        self.store.log("restart.continue", key, {"status": status, "cause": d.get("cause")})
        if status != "fired":
            # A delegated child that will not continue still owes its parent a result (T3 recoverDelegatedTask).
            t = self.store.task_for_child(key)
            if t and t["status"] in ("starting", "running", "waiting_children") and (t["started_at"] or 0) < self._started_at:
                self._finalize(t, {"status": "interrupted", "error": f"The Hermes backend restarted and the task was not "
                                                                     f"continued ({status.replace('_', ' ')})."})
        title = ""
        with contextlib.suppress(Exception):
            title = str((gw.session_row(key, r["profile"]) or {}).get("title") or "")
        summary = restart.SUMMARY_STOPPED if d.get("running") is False else restart.SUMMARY_RESUMED
        self.broadcast("recovery.updated", {"session_key": key, "kind": "restart", "status": status,
                                            "title": title, "summary": summary})

    # ── schedules ───────────────────────────────────────────────────────────
    def schedule_create(self, args: dict, *, bind_key: Optional[str], profile: Optional[str],
                        provider: Optional[str] = None, model: Optional[str] = None, cwd: Optional[str] = None) -> dict:
        prompt = str(args.get("prompt") or "").strip()
        if not prompt:
            raise OrchestratorError("invalid_request", "prompt is required")
        try:
            trig = sched.normalize(args.get("schedule"))
        except ValueError as exc:
            raise OrchestratorError("invalid_request", str(exc)) from exc
        request_id = str(args.get("client_request_id") or "").strip() or None
        if request_id and (existing := self.store.row("SELECT * FROM schedules WHERE client_request_id = ?", (request_id,))):
            return self.schedule_view(existing)
        bind = bind_key if args.get("bind_to_current_thread", True) else None
        signature = args.get("signature") if isinstance(args.get("signature"), dict) else None
        sid = new_id("sch")
        token = new_id("wh") if trig["type"] == "webhook" else None
        row = {
            "id": sid, "title": _clip(str(args.get("title") or prompt.splitlines()[0]), 80), "prompt": prompt,
            "schedule": dumps(trig), "bind_key": bind, "profile": profile, "provider": provider, "model": model, "cwd": cwd,
            "enabled": 1 if args.get("enabled", True) else 0,
            "next_run_at": sched.next_run(trig, after=_now()), "webhook_token": token,
            "signature": dumps(signature), "client_request_id": request_id, "created_at": _now(), "updated_at": _now(),
        }
        self.store.insert("schedules", row)
        if token:
            self.webhooks.ensure(int(self.store.setting("webhook_port")))
        self.broadcast("schedules.updated", {})
        return self.schedule_view(self.store.row("SELECT * FROM schedules WHERE id = ?", (sid,)))

    def schedule_view(self, r: dict) -> dict:
        trig = loads(r["schedule"], {}) or {}
        view = {"id": r["id"], "title": r["title"], "prompt": r["prompt"], "schedule": trig,
                "describe": sched.describe(trig), "enabled": bool(r["enabled"]), "bindSessionKey": r["bind_key"],
                "profile": r["profile"], "nextRunAt": r["next_run_at"], "lastRunAt": r["last_run_at"],
                "lastStatus": r["last_status"], "lastSessionKey": r["last_session_key"], "runCount": r["run_count"],
                "hasSignature": bool(loads(r.get("signature"))), "provider": r["provider"], "model": r["model"]}
        if r["webhook_token"]:
            port = self.webhooks.port or int(self.store.setting("webhook_port"))
            view["webhookUrl"] = f"http://127.0.0.1:{port}/hook/{r['webhook_token']}"
            view["webhookNote"] = ("Loopback URL. To receive internet webhooks, expose it with a tunnel you trust "
                                   "(e.g. `tailscale funnel` or `cloudflared`) and set a signature.")
        return view

    def schedules(self) -> list[dict]:
        return [self.schedule_view(r) for r in self.store.rows("SELECT * FROM schedules ORDER BY created_at")]

    def schedule_update(self, sch_id: str, args: dict) -> dict:
        r = self.store.row("SELECT * FROM schedules WHERE id = ?", (sch_id,))
        if r is None:
            raise OrchestratorError("not_found", f"No schedule {sch_id}.")
        values: dict = {"updated_at": _now()}
        for key in ("title", "prompt"):
            if isinstance(args.get(key), str) and args[key].strip():
                values[key] = args[key].strip()
        if "enabled" in args and args["enabled"] is not None:
            values["enabled"] = 1 if args["enabled"] else 0
        trig = loads(r["schedule"], {})
        if args.get("schedule") is not None:
            try:
                trig = sched.normalize(args["schedule"])
            except ValueError as exc:
                raise OrchestratorError("invalid_request", str(exc)) from exc
            values["schedule"] = dumps(trig)
            values["webhook_token"] = r["webhook_token"] or (new_id("wh") if trig["type"] == "webhook" else None)
        if isinstance(args.get("signature"), dict) or args.get("signature") is None and "signature" in args:
            values["signature"] = dumps(args.get("signature"))
        values["next_run_at"] = sched.next_run(trig, after=_now(), last_run=None)
        self.store.update("schedules", sch_id, values)
        self.broadcast("schedules.updated", {})
        return self.schedule_view(self.store.row("SELECT * FROM schedules WHERE id = ?", (sch_id,)))

    def schedule_delete(self, sch_id: str) -> dict:
        n = self.store.execute("DELETE FROM schedules WHERE id = ?", (sch_id,)).rowcount
        self.broadcast("schedules.updated", {})
        return {"ok": bool(n), "deleted": sch_id}

    def schedule_run(self, sch_id: str, request: Optional[dict] = None) -> dict:
        r = self.store.row("SELECT * FROM schedules WHERE id = ?", (sch_id,))
        if r is None:
            raise OrchestratorError("not_found", f"No schedule {sch_id}.")
        prompt = sched.render_prompt(r["prompt"], request, signed=bool(loads(r.get("signature"))))
        text = f"⏰ Scheduled task: {r['title']}\n\n{prompt}"
        status, key = "queued", r["bind_key"]
        try:
            if r["bind_key"]:
                sid, s = self.live_session(r["bind_key"])
                if sid is not None and not gw.is_running(s) and not self.store.hold(r["bind_key"]) \
                        and self._deliver(r["bind_key"], sid, s, text):
                    status = "started"
                else:
                    self.queue_add(r["bind_key"], text, r["profile"], created_by="schedule")
            else:
                key, sid = self._create_empty(self.unique_title(f"⏰ {r['title']}", r["profile"]), r["profile"], r["provider"] or "", r["model"] or "", r["cwd"])
                gw.submit(sid, text)
                status = "started"
        except Exception as exc:
            status = f"failed: {exc}"
        trig = loads(r["schedule"], {}) or {}
        self.store.update("schedules", sch_id, {
            "last_run_at": _now(), "last_status": status, "last_session_key": key, "run_count": (r["run_count"] or 0) + 1,
            "next_run_at": sched.next_run(trig, after=_now(), last_run=_now()), "updated_at": _now()})
        self.store.log("schedule.run", sch_id, status)
        self.broadcast("schedules.updated", {})
        return {"ok": not status.startswith("failed"), "status": status, "sessionKey": key}

    def _on_webhook(self, token: str, request: dict, raw: bytes) -> tuple[int, dict]:
        r = self.store.row("SELECT * FROM schedules WHERE webhook_token = ?", (token,))
        if r is None:
            return 404, {"error": "unknown webhook"}
        if not r["enabled"]:
            return 409, {"error": "schedule disabled"}
        signature = loads(r.get("signature"))
        if not sched.verify_signature(signature, request.get("headers") or {}, raw):
            return 401, {"error": "bad signature"}
        # Replay protection: one run per distinct delivery within 24h (body + its signature header).
        import hashlib
        headers = {k.lower(): v for k, v in (request.get("headers") or {}).items()}
        sig_value = headers.get(str((signature or {}).get("header", "")).lower(), "") if signature else ""
        delivery = headers.get("x-github-delivery") or headers.get("x-delivery-id") or ""
        digest = hashlib.sha256(raw + b"|" + sig_value.encode() + b"|" + delivery.encode()).hexdigest()
        self.store.execute("DELETE FROM log WHERE kind = 'webhook.seen' AND ts < ?", (_now() - 86400,))
        if self.store.row("SELECT 1 AS x FROM log WHERE kind = 'webhook.seen' AND ref = ?", (digest,)):
            return 200, {"ok": True, "status": "duplicate delivery ignored"}
        self.store.log("webhook.seen", digest, r["id"])
        result = self.schedule_run(r["id"], request)
        return 202, result

    # ── periodic work (engine owner only) ───────────────────────────────────
    def tick(self) -> None:
        if self._paused or not self._claim_owner():
            return
        if not self._boot_done and _now() - self._started_at > 8:
            self._boot_done = True
            try:
                self._boot()
            except Exception:
                logger.warning("t3o boot recovery failed", exc_info=True)
            if self.store.row("SELECT 1 AS x FROM schedules WHERE webhook_token IS NOT NULL AND enabled = 1"):
                self.webhooks.ensure(int(self.store.setting("webhook_port")))
        self._process_cancels()
        self._fire_restarts()
        self._drain_queues()
        self._deliver_results()
        self._deliver_transfers_now()
        self._fire_recoveries()
        self._fire_schedules()
        if _now() - self._last_reconcile > 30:
            self._last_reconcile = _now()
            self._reconcile()
            if self.store.setting("continue_after_restart"):
                self._collect_cut_turns()  # a sibling backend that died since boot

    def _idle_session(self, key: str, profile: Optional[str]) -> tuple[Optional[str], Optional[dict]]:
        sid, s = self.live_session(key)
        if sid is None:
            if self._backoff.get(key, 0) > _now():
                return None, None
            try:
                sid, s = self.ensure_live(key, profile)
            except Exception as exc:
                fails = self._fails.get(key, 0) + 1
                self._fails[key] = fails
                self._backoff[key] = _now() + min(600, 15 * 2 ** min(fails, 5))
                logger.info("t3o: could not open %s (%s); retrying later", key, exc)
                return None, None
            self._fails.pop(key, None)
        if gw.is_running(s) or gw.stop_latched(s) or s.get("queued_prompt"):
            return None, None
        return sid, s

    def _drain_queues(self) -> None:
        for key in self.store.queued_sessions():
            if self.store.hold(key):
                continue
            head = self.store.row("SELECT * FROM queue WHERE session_key = ? AND status = 'pending' "
                                  "ORDER BY position, created_at LIMIT 1", (key,))
            if not head:
                continue
            sid, s = self._idle_session(key, head["profile"])
            if sid is None:
                continue
            with self.store.transaction():
                # Claim the CURRENT head atomically; edits and reorders after this point are refused.
                q = self.store.row("SELECT * FROM queue WHERE session_key = ? AND status = 'pending' "
                                   "ORDER BY position, created_at LIMIT 1", (key,))
                if q:
                    self.store.execute("UPDATE queue SET status = 'sending', updated_at = ? WHERE id = ?", (_now(), q["id"]))
            if not q:
                continue
            if self._deliver(key, sid, s, q["text"]):
                self.store.update("queue", q["id"], {"status": "sent", "sent_at": _now(), "updated_at": _now()})
                self.store.upsert_turn(key, last_user_at=_now())
            else:
                self.store.update("queue", q["id"], {"status": "pending", "updated_at": _now()})
            self._queue_changed(key)

    def _deliver_results(self) -> None:
        by_parent: dict[str, list[dict]] = {}
        for t in self.store.pending_deliveries():
            by_parent.setdefault(t["parent_key"], []).append(t)
        for parent_key, tasks in by_parent.items():
            if self.store.hold(parent_key) or self.store.queue(parent_key):
                continue  # Stop holds wakes; the user's queued messages go first
            sid, s = self._idle_session(parent_key, tasks[0].get("parent_profile"))
            if sid is None:
                continue
            text, meta = self._result_turn(tasks)
            if self._deliver(parent_key, sid, s, text, display_kind=CARD, display_metadata=meta):
                for t in tasks:
                    self.store.update("tasks", t["id"], {"delivery": "delivered", "delivered_at": _now()})
                    self.store.execute("UPDATE transfers SET status = 'consumed', consumed_at = ? WHERE type = 'subagent_result' "
                                       "AND source_key = ? AND status = 'pending'", (_now(), t["child_key"] or ""))
                    self._task_changed(t["id"])

    def _result_turn(self, tasks: list[dict]) -> tuple[str, dict]:
        parts = []
        for t in tasks:
            dur = (t["finished_at"] or _now()) - (t["started_at"] or t["created_at"])
            body = t["result"] or t["error"] or "(no output)"
            head = (f"[T3 delegated task {t['status']}] \"{t['title']}\" — task {t['id']}, "
                    f"{t['provider'] or '?'} / {t['model'] or '?'}, {int(dur)}s, child thread {t['child_key']}")
            if t["status"] == "failed" and t["error"]:
                body = f"Error: {t['error']}\n\n{t['result'] or ''}".strip()
            parts.append(f"{head}\n\n{_clip(body, 30_000)}")
        text = ("Delegated work finished. Use these results to continue; the user sees a summary card.\n\n"
                + "\n\n---\n\n".join(parts))
        n = len(tasks)
        ok = sum(1 for t in tasks if t["status"] == "completed")
        label = tasks[0]["title"] if n == 1 else f"{n} delegated tasks"
        meta = {"display_text": f"↳ {label} — {tasks[0]['status'] if n == 1 else f'{ok}/{n} completed'}",
                "delegation_id": tasks[0]["id"], "task_count": n, "completed_count": ok,
                "failed_count": n - ok, "duration_seconds": round(max(((t['finished_at'] or 0) - (t['started_at'] or t['created_at'])) for t in tasks), 1),
                "t3o": True}
        return text, meta

    def _deliver_transfers_now(self) -> None:
        rows = self.store.rows("SELECT * FROM transfers WHERE status = 'pending' AND delivery = 'now' "
                               "AND type IN ('fork','merge_back') ORDER BY created_at")
        for x in rows:
            if not x["target_key"] or self.store.hold(x["target_key"]):
                continue
            sid, s = self._idle_session(x["target_key"], x["profile"])
            if sid is None:
                continue
            label = "Context handoff" if x["type"] == "fork" else "Merged context from fork"
            text = (x["handoff"] or "") + "\n\nAcknowledge this context in one or two sentences; do not start new work."
            meta = {"display_text": f"⇄ {label}", "delegation_id": x["id"], "task_count": 1, "completed_count": 1,
                    "failed_count": 0, "t3o": True}
            if self._deliver(x["target_key"], sid, s, text, display_kind=CARD, display_metadata=meta):
                self.store.execute("UPDATE transfers SET status = 'consumed', consumed_at = ? WHERE id = ?", (_now(), x["id"]))
                self.broadcast("lineage.updated", {"session_key": x["target_key"]})

    def _fire_recoveries(self) -> None:
        if not self.store.setting("resume_on_usage_limit"):
            return
        grace = float(self.store.setting("usage_limit_grace_s"))
        for r in self.store.rows("SELECT * FROM recoveries WHERE status = 'armed' AND kind = 'usage_limit' AND reset_at <= ?",
                                 (_now() - grace,)):
            turn = self.store.turn(r["session_key"]) or {}
            if (turn.get("last_user_at") or 0) > r["created_at"]:
                self.store.update("recoveries", r["id"], {"status": "superseded"})  # the user already moved on
                continue
            if self.store.hold(r["session_key"]):
                continue
            sid, s = self._idle_session(r["session_key"], r["profile"])
            if sid is None:
                continue
            if self._deliver(r["session_key"], sid, s, CONTINUE_PROMPT, display_kind="auto_continue"):
                self.store.update("recoveries", r["id"], {"status": "fired", "fired_at": _now(),
                                                          "attempts": int(r.get("attempts") or 0) + 1})
                task = self.store.task_for_child(r["session_key"])
                if task and task["status"] == "waiting" and self.store.update(
                        "tasks", task["id"], {"status": "running", "error": None},
                        where="status = 'waiting' AND cancel_requested = 0"):
                    self._task_changed(task["id"])
                self.broadcast("recovery.updated", {"session_key": r["session_key"]})

    def _fire_schedules(self) -> None:
        for r in self.store.rows("SELECT * FROM schedules WHERE enabled = 1 AND next_run_at IS NOT NULL AND next_run_at <= ?",
                                 (_now(),)):
            try:
                self.schedule_run(r["id"])
            except Exception:
                logger.warning("t3o schedule %s failed", r["id"], exc_info=True)

    def _reconcile(self) -> None:
        """Repair tasks whose terminal frame was missed (e.g. the session closed between turns)."""
        for t in self.store.active_tasks():
            if t["status"] != "running" or not t["child_key"]:
                continue
            sid, s = self.live_session(t["child_key"])
            if sid is not None and gw.is_running(s):
                continue
            idle_for = _now() - max(t["started_at"] or 0, (loads(t["progress"], {}) or {}).get("at") or 0)
            if idle_for < 90:
                continue
            msgs = []
            with contextlib.suppress(Exception):
                msgs = gw.history(t["child_key"], t["profile"])
            rs = handoff.runs(msgs)
            if rs and rs[-1]["status"] == "completed" and len(rs) >= 1:
                self._finalize(t, {"status": "completed", "result": _clip(handoff.last_reply(msgs), 60_000)})
            elif sid is None:
                self._finalize(t, {"status": "interrupted", "error": "The child session closed before finishing."})
            elif idle_for > 900:
                self._finalize(t, {"status": "interrupted", "error": "The child session stopped without a final reply.",
                                   "result": _clip(handoff.last_reply(msgs), 20_000) or None})


_ENGINE: Optional[Engine] = None
_ENGINE_LOCK = threading.Lock()


def engine() -> Engine:
    global _ENGINE
    with _ENGINE_LOCK:
        if _ENGINE is None:
            _ENGINE = Engine()
        return _ENGINE
