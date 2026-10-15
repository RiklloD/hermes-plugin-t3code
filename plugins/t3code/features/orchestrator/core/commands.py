"""``/t3`` slash command: manage orchestration from any surface, including messaging apps on a phone.

Runs in whichever process received the command (Desktop backend or the messaging gateway). Reads and
schedule/queue/setting edits go to the shared store; the engine in ``hermes serve`` acts on them.
"""

from __future__ import annotations

import re
import shlex
import time
from datetime import datetime

from .engine import OrchestratorError, engine
from .schedule import describe

HELP = """/t3 — T3 orchestrator
  /t3 status                       active tasks, armed recoveries, schedules
  /t3 tasks                        recent delegated tasks
  /t3 cancel <task_id>             stop a task and its subtasks
  /t3 schedules                    list scheduled tasks
  /t3 schedule every <N><m|h|d> <prompt>
  /t3 schedule daily <HH:MM> [weekdays|mon,wed,…] <prompt>
  /t3 schedule pause|resume|run|delete <id>
  /t3 settings [key value]         e.g. /t3 settings resume_on_usage_limit off"""

_DAYS = {"sun": 0, "mon": 1, "tue": 2, "wed": 3, "thu": 4, "fri": 5, "sat": 6}


def _when(ts) -> str:
    if not ts:
        return "—"
    return datetime.fromtimestamp(ts).strftime("%a %H:%M")


def _status() -> str:
    eng = engine()
    active = eng.store.active_tasks()
    lines = [f"Active tasks: {len(active)}"]
    for t in active[:12]:
        lines.append(f"  • {t['id']} [{t['status']}] {t['title']} — {t['provider']}/{t['model']}")
    recs = [r for r in eng.recoveries() if r["status"] == "armed" and r["kind"] != "restart"]
    restarts = [r for r in eng.recoveries() if r["kind"] == "restart" and r["status"] in ("armed", "fired")]
    if restarts:
        lines.append(f"Restart continuations (24h): {len(restarts)}")
    lines.append(f"Usage-limit resumes armed: {len(recs)}")
    for r in recs[:6]:
        lines.append(f"  • {r['session_key']} resumes {_when(r['reset_at'])}")
    sch = eng.schedules()
    lines.append(f"Schedules: {len(sch)} ({sum(1 for s in sch if s['enabled'])} enabled)")
    s = eng.store.settings()
    lines.append(f"Continue after restart: {'on' if s['continue_after_restart'] else 'off'} · "
                 f"Resume on usage limit: {'on' if s['resume_on_usage_limit'] else 'off'}")
    return "\n".join(lines)


def _tasks() -> str:
    rows = engine().store.recent_tasks(15)
    if not rows:
        return "No delegated tasks yet."
    out = []
    for t in rows:
        dur = int((t["finished_at"] or time.time()) - (t["started_at"] or t["created_at"]))
        out.append(f"• {t['id']} [{t['status']}] {t['title']} — {t['provider']}/{t['model']} · {dur}s")
    return "\n".join(out)


def _schedules() -> str:
    rows = engine().schedules()
    if not rows:
        return "No schedules."
    return "\n".join(f"• {s['id']} {'▶' if s['enabled'] else '⏸'} {s['title']} — {s['describe']} · next {_when(s['nextRunAt'])}"
                     for s in rows)


def _schedule(args: list[str]) -> str:
    eng = engine()
    if not args:
        return HELP
    verb = args[0].lower()
    if verb in ("pause", "resume", "run", "delete") and len(args) >= 2:
        sid = args[1]
        if verb == "delete":
            return "Deleted." if eng.schedule_delete(sid)["ok"] else "No such schedule."
        if verb == "run":
            return f"Ran: {eng.schedule_run(sid)['status']}"
        eng.schedule_update(sid, {"enabled": verb == "resume"})
        return f"Schedule {sid} {'resumed' if verb == 'resume' else 'paused'}."
    if verb == "every" and len(args) >= 3:
        m = re.fullmatch(r"(\d+)\s*([mhd])", args[1].lower())
        if not m:
            return "Use e.g. /t3 schedule every 30m <prompt>"
        ms = int(m.group(1)) * {"m": 60_000, "h": 3_600_000, "d": 86_400_000}[m.group(2)]
        trig = {"type": "interval", "everyMs": ms}
        prompt = " ".join(args[2:])
    elif verb == "daily" and len(args) >= 3:
        trig = {"type": "fixed_time", "timeOfDay": args[1]}
        rest = args[2:]
        if rest and (rest[0].lower() == "weekdays" or all(d[:3] in _DAYS for d in rest[0].lower().split(","))):
            trig["weekdays"] = [1, 2, 3, 4, 5] if rest[0].lower() == "weekdays" else \
                sorted({_DAYS[d[:3]] for d in rest[0].lower().split(",")})
            rest = rest[1:]
        prompt = " ".join(rest)
        if not prompt:
            return "A prompt is required."
    else:
        return HELP
    view = eng.schedule_create({"prompt": prompt, "schedule": trig, "bind_to_current_thread": False},
                               bind_key=None, profile=None)
    return f"Scheduled {view['id']}: {view['describe']}, next run {_when(view['nextRunAt'])}. Each run starts a new thread."


def _settings(args: list[str]) -> str:
    store = engine().store
    if len(args) >= 2:
        raw = args[1].lower()
        value = raw in ("on", "true", "1", "yes") if raw in ("on", "off", "true", "false", "1", "0", "yes", "no") else args[1]
        store.set_settings({args[0]: value})
    return "\n".join(f"{k}: {v}" for k, v in store.settings().items())


def handle(raw_args: str) -> str:
    try:
        args = shlex.split(raw_args or "")
    except ValueError:
        args = (raw_args or "").split()
    cmd = (args[0].lower() if args else "status")
    try:
        if cmd == "status":
            return _status()
        if cmd == "tasks":
            return _tasks()
        if cmd == "cancel" and len(args) >= 2:
            t = engine().cancel(args[1], "cancelled from /t3")
            return f"{t['taskId']}: {t['status']}{' (stop requested)' if t['cancelRequested'] else ''}"
        if cmd == "schedules":
            return _schedules()
        if cmd == "schedule":
            return _schedule(args[1:])
        if cmd == "settings":
            return _settings(args[1:])
        return HELP
    except (OrchestratorError, ValueError) as exc:
        return f"t3: {exc}"
