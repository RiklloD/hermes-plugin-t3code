"""T3 Code · Orchestrator REST for the Desktop chat UI and the web dashboard tab.

Mounted at ``/api/plugins/t3code/orchestrator/`` by ``dashboard/plugin_api.py``.
"""

from __future__ import annotations

import contextlib
import importlib
import importlib.util
import sys
from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, Body, HTTPException, Query

router = APIRouter()
_HERE = Path(__file__).resolve().parent  # features/orchestrator


def _core():
    if "t3o" not in sys.modules:
        spec = importlib.util.spec_from_file_location("t3o", _HERE / "core" / "__init__.py",
                                                      submodule_search_locations=[str(_HERE / "core")])
        mod = importlib.util.module_from_spec(spec)
        sys.modules["t3o"] = mod
        spec.loader.exec_module(mod)
    eng = importlib.import_module("t3o.engine")
    gw = importlib.import_module("t3o.gw")
    e = eng.engine()
    e.start()
    return eng, gw, e


def _run(fn, *args, **kwargs) -> Any:
    eng, gw, _ = _core()
    try:
        return fn(*args, **kwargs)
    except eng.OrchestratorError as exc:
        raise HTTPException(status_code=400, detail={"error": exc.code, "message": str(exc)})
    except gw.GatewayError as exc:
        raise HTTPException(status_code=409, detail={"error": "gateway_error", "message": str(exc)})


_profile_cache: dict[str, Optional[str]] = {}


def _profile_for(key: str, given: Optional[str]) -> Optional[str]:
    if given:
        return None if given == "default" else given
    if key in _profile_cache:
        return _profile_cache[key]
    _, gw, e = _core()
    _, s = gw.live_for_key(key)
    profile = gw.profile_of(s) if s else None
    if s is None:
        import sqlite3
        for name, home in e._profiles():
            db = home / "state.db"
            if not db.exists():
                continue
            with contextlib.suppress(Exception):
                con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=5)
                try:
                    if con.execute("SELECT 1 FROM sessions WHERE id = ?", (key,)).fetchone():
                        profile = name
                        break
                finally:
                    con.close()
    _profile_cache[key] = profile
    return profile


def _title(key, profile):
    if not key:
        return None
    _, gw, _ = _core()
    with contextlib.suppress(Exception):
        return (gw.session_row(key, None if profile in (None, "default") else profile) or {}).get("title")
    return None


@router.get("/overview")
def overview(session_key: str = Query(...), profile: Optional[str] = None):
    _, gw, e = _core()
    p = _profile_for(session_key, profile)
    as_child = e.store.task_for_child(session_key)
    sid, s = gw.live_for_key(session_key)
    return {
        "sessionKey": session_key, "profile": p or "default",
        "live": sid is not None, "running": bool(s and s.get("running")),
        "tasks": [e.task_view(t) for t in e.store.tasks_for_parent(session_key)],
        "asChild": e.task_view(as_child) if as_child else None,
        "queue": e.queue_list(session_key),
        "lineage": e.store.lineage(session_key),
        "forkedFromTitle": _title((e.store.fork_origin(session_key) or {}).get("source_key"), p),
        "parentTitle": _title(as_child["parent_key"], as_child.get("parent_profile")) if as_child else None,
        "pendingTransfers": [{k: x[k] for k in ("id", "type", "source_key", "delivery", "created_at")}
                             for x in e.store.pending_transfers_for(session_key)],
        "recoveries": e.recoveries(session_key),
        "settings": e.store.settings(),
        "engine": {"owner": e._owner, "attached": gw.is_serve_process()},
    }


@router.get("/tasks")
def tasks(scope: str = "recent", limit: int = 100):
    _, _, e = _core()
    rows = e.store.active_tasks() if scope == "active" else e.store.recent_tasks(max(1, min(limit, 500)))
    return {"tasks": [e.task_view(t) for t in rows]}


@router.get("/tasks/{task_id}")
def task(task_id: str):
    _, _, e = _core()
    t = e.store.task(task_id)
    if t is None:
        raise HTTPException(404, "no such task")
    return {**e.task_view(t), "prompt": t["prompt"], "result": t["result"]}


@router.post("/tasks/{task_id}/cancel")
def cancel(task_id: str):
    _, _, e = _core()
    return _run(e.cancel, task_id, "cancelled by the user")


@router.post("/sessions/{session_key}/stop-background")
def stop_background(session_key: str):
    _, _, e = _core()
    return {"ok": True, "stopped": _run(e.stop_descendants, session_key, "stopped by the user")}


@router.get("/runs/{session_key}")
def runs(session_key: str, profile: Optional[str] = None):
    _, _, e = _core()
    return {"runs": _run(e.runs, session_key, _profile_for(session_key, profile))}


@router.post("/fork")
def fork(body: dict = Body(...)):
    _, _, e = _core()
    key = str(body.get("session_key") or "")
    run = body.get("run") if isinstance(body.get("run"), int) else None
    target = body.get("target") if isinstance(body.get("target"), dict) else None
    return _run(e.fork, key, _profile_for(key, body.get("profile")), run=run, title=str(body.get("title") or ""),
                target=target, created_by="user")


@router.post("/merge")
def merge(body: dict = Body(...)):
    _, _, e = _core()
    key = str(body.get("fork_key") or "")
    run = body.get("run") if isinstance(body.get("run"), int) else None
    return _run(e.merge_back, key, _profile_for(key, body.get("profile")), target_key=body.get("target_key") or None,
                run=run, delivery=str(body.get("delivery") or "auto"), created_by="user")


@router.get("/queue/{session_key}")
def queue_list(session_key: str):
    _, _, e = _core()
    return e.queue_list(session_key)


@router.post("/queue/{session_key}")
def queue_action(session_key: str, body: dict = Body(...), profile: Optional[str] = None):
    _, _, e = _core()
    action = body.get("action")
    qid = str(body.get("id") or "")
    p = _profile_for(session_key, profile or body.get("profile"))
    if action == "add":
        return _run(e.queue_add, session_key, str(body.get("text") or ""), p)
    if action == "edit":
        return _run(e.queue_edit, session_key, qid, str(body.get("text") or ""))
    if action == "reorder":
        return _run(e.queue_reorder, session_key, qid, body.get("before_id") or None)
    if action == "remove":
        return _run(e.queue_remove, session_key, qid)
    if action == "clear":
        return _run(e.queue_clear, session_key)
    if action in ("hold", "release"):
        return _run(e.queue_hold, session_key, action == "hold")
    if action == "promote":
        return _run(e.queue_promote, session_key, qid, str(body.get("how") or "steer"))
    if action == "send":
        return _run(e.send, session_key, p, str(body.get("text") or ""), str(body.get("mode") or "auto"))
    raise HTTPException(400, "unknown action")


@router.get("/recoveries")
def recoveries():
    _, _, e = _core()
    return {"recoveries": e.recoveries()}


@router.post("/recoveries/{rec_id}/cancel")
def cancel_recovery(rec_id: str):
    _, _, e = _core()
    e.cancel_recovery(rec_id)
    return {"ok": True}


@router.get("/schedules")
def schedules():
    _, _, e = _core()
    return {"schedules": e.schedules()}


@router.post("/schedules")
def create_schedule(body: dict = Body(...)):
    _, gw, e = _core()
    key = str(body.get("session_key") or "") or None
    p = _profile_for(key, body.get("profile")) if key else (body.get("profile") or None)
    provider = model = cwd = None
    if key:
        _, s = gw.live_for_key(key)
        agent = (s or {}).get("agent")
        provider, model = getattr(agent, "provider", None), getattr(agent, "model", None)
        cwd = str((s or {}).get("cwd") or "") or None
    args = {**body, "bind_to_current_thread": bool(key) and body.get("bind_to_current_thread", True)}
    return _run(e.schedule_create, args, bind_key=key, profile=p, provider=body.get("provider") or provider,
                model=body.get("model") or model, cwd=cwd)


@router.patch("/schedules/{sch_id}")
def update_schedule(sch_id: str, body: dict = Body(...)):
    _, _, e = _core()
    return _run(e.schedule_update, sch_id, body)


@router.delete("/schedules/{sch_id}")
def delete_schedule(sch_id: str):
    _, _, e = _core()
    return e.schedule_delete(sch_id)


@router.post("/schedules/{sch_id}/run")
def run_schedule(sch_id: str):
    _, _, e = _core()
    return _run(e.schedule_run, sch_id)


@router.get("/settings")
def get_settings():
    _, _, e = _core()
    return e.store.settings()


@router.post("/settings")
def set_settings(body: dict = Body(...)):
    _, _, e = _core()
    try:
        return e.store.set_settings(body)
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@router.get("/profiles")
def profiles():
    _, _, e = _core()
    return {"profiles": e.profiles()}


@router.get("/catalog")
def catalog(profile: Optional[str] = None):
    _, _, e = _core()
    return {"providers": _run(e.catalog, None if profile in (None, "", "default") else profile)}


@router.get("/log")
def log(limit: int = 100):
    _, _, e = _core()
    return {"log": e.store.rows("SELECT * FROM log ORDER BY id DESC LIMIT ?", (max(1, min(limit, 500)),))}
