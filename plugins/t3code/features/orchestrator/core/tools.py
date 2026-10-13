"""Model-facing tools (T3's orchestrator MCP surface, adapted to Hermes sessions)."""

from __future__ import annotations

import json
from typing import Any, Callable

from . import gw
from .engine import ROLES, OrchestratorError, engine

TOOLSET = "t3_orchestrator"

_TARGET = {
    "type": "object",
    "description": "Provider/model for the child. Omit to inherit this thread's. Pick from t3_capabilities.",
    "properties": {
        "provider": {"type": "string", "description": "Provider slug, e.g. openai-codex, anthropic, claude-cli, nous, zai."},
        "model": {"type": "string", "description": "A model advertised by that provider. Provider alone picks its first model."},
        "reasoning_effort": {"type": "string", "description": "Optional effort, e.g. low, medium, high."},
        "profile": {"type": "string", "description": "t3_delegate only: run the child as ANOTHER Hermes profile "
                    "(its own memory, skills, tools, config and default model; see profiles in t3_capabilities). "
                    "Omit provider/model to use that profile's default."},
    },
}

SCHEMAS: dict[str, dict] = {
    "t3_capabilities": {
        "name": "t3_capabilities",
        "description": (
            "T3 orchestrator: list the providers and models this thread can delegate to (the same live catalog as the "
            "model picker), the inherited provider/model, the Hermes profiles you can delegate to (each runs as itself), "
            "and orchestration features and limits. Pass profile to list that profile's providers/models. Call before "
            "choosing a target for t3_delegate."
        ),
        "parameters": {"type": "object", "properties": {"profile": {"type": "string"}}},
    },
    "t3_delegate": {
        "name": "t3_delegate",
        "description": (
            "T3 orchestrator: delegate one task to a child agent on ANY provider/model (e.g. Claude plans, Codex "
            "implements), or to ANOTHER Hermes profile with target.profile (e.g. ask another profile you have set up). The child is a real, visible Hermes session that gets ONLY the task text, never this thread's "
            "history, so include every fact, path and constraint it needs. Use this instead of delegate_task when the "
            "child needs a different provider or model, or should be a visible, steerable thread. mode='async' (default) "
            "returns a taskId immediately; the result is delivered back to this thread as a new turn when it finishes, "
            "so end your turn instead of polling. mode='wait' blocks until done or timeout_s (the child keeps running "
            "on timeout). For each review round, call t3_delegate again with the full brief, prior findings and "
            "objections, and a new client_request_id."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "task": {"type": "string", "description": "Complete, self-contained brief for the child."},
                "title": {"type": "string", "description": "Short label shown in the UI."},
                "role": {"type": "string", "enum": list(ROLES)},
                "target": _TARGET,
                "mode": {"type": "string", "enum": ["async", "wait"]},
                "timeout_s": {"type": "number", "description": "Wait budget for mode=wait (default 600)."},
                "client_request_id": {"type": "string", "description": "Stable per logical request; retries return the same task."},
            },
            "required": ["task"],
        },
    },
    "t3_task": {
        "name": "t3_task",
        "description": (
            "T3 orchestrator: inspect or stop delegated tasks of this thread. action=status reads one task (status, "
            "workState, provider/model, progress, duration, summary = final result or error); reading a finished result "
            "acknowledges its automatic delivery. action=list lists this thread's tasks. action=cancel stops the child "
            "and everything it delegated."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["status", "list", "cancel"]},
                "task_id": {"type": "string"},
                "reason": {"type": "string"},
            },
            "required": ["action"],
        },
    },
    "t3_thread": {
        "name": "t3_thread",
        "description": (
            "T3 orchestrator thread operations. action=runs lists a thread's runs (turns) with status, including failed "
            "or interrupted ones. action=fork branches a thread from a run (default: latest) into a new thread; Claude CLI "
            "threads fork natively, Hermes threads copy their transcript, other targets get a portable context handoff; "
            "target may pick a different provider/model. action=merge_back brings a fork's work since the fork point "
            "back into its source thread (or target_thread). action=lineage shows forks and merges. action=send messages "
            "a thread: mode auto (start idle / steer active), queue (durable follow-up), steer (inject into the active "
            "turn) or restart (redirect the active turn). Omit thread to act on this thread."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["runs", "fork", "merge_back", "lineage", "send"]},
                "thread": {"type": "string", "description": "Stored session id; defaults to this thread."},
                "run": {"type": "integer", "description": "Run ordinal (1-based) for fork/merge_back; default latest."},
                "title": {"type": "string"},
                "target": _TARGET,
                "target_thread": {"type": "string", "description": "merge_back destination; default the fork's source."},
                "delivery": {"type": "string", "enum": ["auto", "next_message", "now"],
                             "description": "merge_back: inject with the next message, or as a context turn now."},
                "message": {"type": "string"},
                "mode": {"type": "string", "enum": ["auto", "queue", "steer", "restart"]},
            },
            "required": ["action"],
        },
    },
    "t3_queue": {
        "name": "t3_queue",
        "description": (
            "T3 orchestrator durable message queue for a thread: follow-ups that survive restarts and run in order when "
            "the thread is idle. Actions: list, add (text), edit (id, text), reorder (id, before_id or null = end), "
            "remove (id), clear, hold / release (pause or resume draining), promote (id; deliver into the active turn "
            "as a steer, or how='restart' to redirect it). A user Stop holds the queue until the next message."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["list", "add", "edit", "reorder", "remove", "clear", "hold",
                                                       "release", "promote"]},
                "thread": {"type": "string"},
                "id": {"type": "string"},
                "text": {"type": "string"},
                "before_id": {"type": ["string", "null"]},
                "how": {"type": "string", "enum": ["steer", "restart"]},
            },
            "required": ["action"],
        },
    },
    "t3_schedule": {
        "name": "t3_schedule",
        "description": (
            "T3 orchestrator scheduled tasks that run even when no turn is active. action=create with prompt and "
            "schedule as an OBJECT: {type:'interval', everyMs:3600000}, {type:'fixed_time', timeOfDay:'09:00', "
            "weekdays:[1,2,3,4,5]} (0=Sunday, local time) or {type:'webhook'} (runs per request to the returned "
            "loopback webhookUrl; prompt placeholders {{body.path}}, {{headers.name}}, {{query.name}}, {{body}}, "
            "{{request}}; optional signature {header, encoding:'hex'|'base64', prefix, secret} for HMAC-SHA256). Runs "
            "post into THIS thread by default (bind_to_current_thread=true; queued if busy); false starts a fresh "
            "thread per run. Report nextRunAt. Also: list, update (id + fields), delete (id), run_now (id)."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["create", "list", "update", "delete", "run_now"]},
                "id": {"type": "string"},
                "prompt": {"type": "string"},
                "title": {"type": "string"},
                "schedule": {"type": "object"},
                "bind_to_current_thread": {"type": "boolean"},
                "enabled": {"type": "boolean"},
                "signature": {"type": ["object", "null"]},
                "client_request_id": {"type": "string"},
            },
            "required": ["action"],
        },
    },
}


def _ok(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, default=str)


def _guard(fn: Callable[..., Any]) -> Callable[..., str]:
    def handler(args: dict, **kwargs: Any) -> str:
        try:
            return _ok(fn(args or {}, **kwargs))
        except OrchestratorError as exc:
            return _ok(exc.to_dict())
        except gw.GatewayError as exc:
            return _ok({"ok": False, "error": "gateway_error", "code": exc.code, "message": str(exc)})
        except Exception as exc:  # never raise into the agent loop
            return _ok({"ok": False, "error": "orchestration_error", "message": f"{type(exc).__name__}: {exc}"})
    handler.__name__ = fn.__name__
    return handler


def _ctx(kwargs: dict) -> tuple[str, Any]:
    """(this thread's stored key, its profile)"""
    eng = engine()
    _, session, key = eng.resolve_parent(kwargs.get("parent_agent"), str(kwargs.get("session_id") or ""))
    return key, gw.profile_of(session)


def _thread(args: dict, kwargs: dict) -> tuple[str, Any, str]:
    key, profile = _ctx(kwargs)
    return str(args.get("thread") or key), profile, key


@_guard
def t3_capabilities(args: dict, **kwargs: Any) -> Any:
    return engine().capabilities(kwargs.get("parent_agent"), for_profile=str(args.get("profile") or "") or None)


@_guard
def t3_delegate(args: dict, **kwargs: Any) -> Any:
    return engine().delegate(args, kwargs.get("parent_agent"), str(kwargs.get("session_id") or ""))


@_guard
def t3_task(args: dict, **kwargs: Any) -> Any:
    eng = engine()
    key, _ = _ctx(kwargs)
    action = args.get("action")
    if action == "list":
        return {"tasks": [eng.task_view(t) for t in eng.store.tasks_for_parent(key)]}
    task_id = str(args.get("task_id") or "")
    if not task_id:
        raise OrchestratorError("invalid_request", "task_id is required")
    if action == "status":
        return eng.task_status(task_id, key)
    if action == "cancel":
        return eng.cancel(task_id, str(args.get("reason") or "cancelled by parent agent"), key)
    raise OrchestratorError("invalid_request", "action must be status, list or cancel")


@_guard
def t3_thread(args: dict, **kwargs: Any) -> Any:
    eng = engine()
    thread, profile, me = _thread(args, kwargs)
    action = args.get("action")
    run = args.get("run") if isinstance(args.get("run"), int) else None
    if action == "runs":
        return {"thread": thread, "runs": eng.runs(thread, profile)}
    if action == "fork":
        return eng.fork(thread, profile, run=run, title=str(args.get("title") or ""),
                        target=args.get("target") if isinstance(args.get("target"), dict) else None, created_by="agent")
    if action == "merge_back":
        return eng.merge_back(thread, profile, target_key=args.get("target_thread") or None, run=run,
                              delivery=str(args.get("delivery") or "auto"), created_by="agent")
    if action == "lineage":
        return {"thread": thread, **eng.store.lineage(thread)}
    if action == "send":
        if thread == me and str(args.get("mode") or "auto") in ("auto", "steer", "restart"):
            raise OrchestratorError("invalid_request", "Use mode='queue' to send a follow-up to this same thread.")
        return eng.send(thread, profile, str(args.get("message") or ""), str(args.get("mode") or "auto"))
    raise OrchestratorError("invalid_request", "action must be runs, fork, merge_back, lineage or send")


@_guard
def t3_queue(args: dict, **kwargs: Any) -> Any:
    eng = engine()
    thread, profile, _ = _thread(args, kwargs)
    action = args.get("action")
    qid = str(args.get("id") or "")
    if action == "list":
        return eng.queue_list(thread)
    if action == "add":
        return eng.queue_add(thread, str(args.get("text") or ""), profile, created_by="agent")
    if action == "clear":
        return eng.queue_clear(thread)
    if action in ("hold", "release"):
        return eng.queue_hold(thread, action == "hold")
    if not qid:
        raise OrchestratorError("invalid_request", "id is required")
    if action == "edit":
        return eng.queue_edit(thread, qid, str(args.get("text") or ""))
    if action == "reorder":
        return eng.queue_reorder(thread, qid, args.get("before_id") or None)
    if action == "remove":
        return eng.queue_remove(thread, qid)
    if action == "promote":
        return eng.queue_promote(thread, qid, str(args.get("how") or "steer"))
    raise OrchestratorError("invalid_request", f"unknown action {action}")


@_guard
def t3_schedule(args: dict, **kwargs: Any) -> Any:
    eng = engine()
    action = args.get("action")
    if action == "list":
        return {"schedules": eng.schedules()}
    if action == "create":
        key, profile = _ctx(kwargs)
        agent = kwargs.get("parent_agent")
        _, session = gw.live_for_key(key)
        return eng.schedule_create(args, bind_key=key, profile=profile,
                                   provider=getattr(agent, "provider", None), model=getattr(agent, "model", None),
                                   cwd=str((session or {}).get("cwd") or "") or None)
    sch_id = str(args.get("id") or "")
    if not sch_id:
        raise OrchestratorError("invalid_request", "id is required")
    if action == "update":
        return eng.schedule_update(sch_id, args)
    if action == "delete":
        return eng.schedule_delete(sch_id)
    if action == "run_now":
        return eng.schedule_run(sch_id)
    raise OrchestratorError("invalid_request", "action must be create, list, update, delete or run_now")


HANDLERS: dict[str, Callable[..., str]] = {
    "t3_capabilities": t3_capabilities,
    "t3_delegate": t3_delegate,
    "t3_task": t3_task,
    "t3_thread": t3_thread,
    "t3_queue": t3_queue,
    "t3_schedule": t3_schedule,
}
