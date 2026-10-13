"""T3 Code · Inbox backend: what the Desktop inbox needs that the session list does not carry.

Mounted at ``/api/plugins/t3code/inbox/`` by ``dashboard/plugin_api.py``.

Routes:
  GET /outcomes       last-turn outcome + timestamps per session, usage-limit reset time
  GET /interrupted    turns a dead process left behind (crash markers), per profile
  GET/POST /lifecycle shared settle / snooze / Woke records (newest write wins)

Token usage and live limits are the Usage feature's (``features/usage/api.py``).
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import sqlite3
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Optional

from fastapi import APIRouter, Query

from t3code_shared.hermes_data import hermes_root, positive_int as _int, profile_dbs as _profile_dbs

router = APIRouter()

# ─── last-turn outcome (inbox status) ───────────────────────────────────────
# Hermes' own transcript boundaries (agent/turn_failure_copy.py) and the
# interrupt notice. An interrupt is the user stopping the turn — history, not
# a failure — so it gets its own outcome.
_FAILED_PREFIXES = (
    "Your request was not processed",
    "This turn did not complete",
)
_STOPPED_PREFIXES = ("Operation interrupted",)
# Hermes does not persist the failure reason, so a usage limit is only
# recognisable when the failing reply itself says so.
_LIMIT_MARKERS = ("usage limit", "rate limit", "rate-limit", "quota", "limit reached", "429")


_UNIT_MS = {"d": 86_400_000, "h": 3_600_000, "m": 60_000, "s": 1_000}
_DURATION_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(days?|d|hours?|hrs?|h|minutes?|mins?|m|seconds?|secs?|s)\b", re.I)
_CLOCK_RE = re.compile(r"(?:reset|resets|try again|retry)\s*(?:at|after|on)?\s*(\d{1,2})(?::(\d{2}))?\s*(am|pm)?(?:\s*\(([A-Za-z_]+/[A-Za-z_]+)\))?", re.I)
_EPOCH_RE = re.compile(r"\|\s*(\d{10})(?!\d)")
_ISO_RE = re.compile(r"\b(\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(?::\d{2})?(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?)")


def _parse_limit_reset(text: Any, at_ms: Optional[int]) -> Optional[int]:
    """When a usage-limit reply says the limit lifts (epoch ms), or None.

    Hermes keeps no structured failure, so this reads what providers actually print:
    Claude's ``...limit reached|<epoch>`` / ``resets 3pm (Europe/Paris)``, Codex's
    ``try again in 4 days 2 hours`` / ``try again at 3:45 PM``, ISO timestamps and
    ``retry after N seconds``. Relative phrases count from the reply's own timestamp."""
    if not isinstance(text, str) or not text:
        return None
    base = at_ms or int(time.time() * 1000)
    if m := _EPOCH_RE.search(text):
        return int(m.group(1)) * 1000
    if m := _ISO_RE.search(text):
        raw = m.group(1).replace(" ", "T").replace("Z", "+00:00")
        try:
            dt = datetime.fromisoformat(raw)
            if dt.tzinfo is None:
                dt = dt.astimezone()
            return int(dt.timestamp() * 1000)
        except ValueError:
            pass
    rel = re.search(r"(?:in|after|for)\s+((?:\d+(?:\.\d+)?\s*[a-z]+\s*(?:,|and)?\s*)+)", text, re.I)
    if rel:
        total = sum(float(n) * _UNIT_MS[u[0].lower()] for n, u in _DURATION_RE.findall(rel.group(1)))
        if total > 0:
            return int(base + total)
    if m := _CLOCK_RE.search(text):
        hour, minute, ampm, tz = int(m.group(1)), int(m.group(2) or 0), (m.group(3) or "").lower(), m.group(4)
        if ampm == "pm" and hour < 12:
            hour += 12
        elif ampm == "am" and hour == 12:
            hour = 0
        if hour > 23 or minute > 59:
            return None
        try:
            from zoneinfo import ZoneInfo
            zone = ZoneInfo(tz) if tz else None
        except Exception:
            zone = None
        start = datetime.fromtimestamp(base / 1000, zone) if zone else datetime.fromtimestamp(base / 1000).astimezone()
        due = start.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if due <= start:
            due += timedelta(days=1)
        return int(due.timestamp() * 1000)
    return None


def _classify_outcome(kind: Any, finish: Any, content: Any) -> str:
    text = content.strip() if isinstance(content, str) else ""
    if text.startswith(_STOPPED_PREFIXES):
        return "stopped"
    if kind == "failed_turn" or finish == "error" or text.startswith(_FAILED_PREFIXES):
        lowered = text.lower()
        return "limited" if any(m in lowered for m in _LIMIT_MARKERS) else "failed"
    return "ok"


def _outcomes(limit: int) -> Dict[str, Dict[str, Dict[str, Any]]]:
    """``{profile: {session_id: {outcome, at, sent_at}}}`` for each profile's most recent sessions.

    ``outcome`` (``ok`` | ``failed`` | ``limited`` | ``stopped``) and ``at`` come from the
    session's last chat message when it is an assistant reply; a trailing user message has no
    outcome yet. ``sent_at`` is the last user-authored message: the inbox orders Working rows
    by it so streaming output never reshuffles them."""
    out: Dict[str, Dict[str, Dict[str, Any]]] = {}
    for profile, path in _profile_dbs():
        try:
            con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=2)
        except sqlite3.Error:
            continue
        try:
            rows = con.execute(
                """
                WITH recent AS (
                    SELECT id FROM sessions
                    ORDER BY COALESCE(last_activity_at, started_at) DESC LIMIT ?
                )
                SELECT m.session_id, m.role, m.display_kind, m.finish_reason, m.content, m.timestamp,
                       (SELECT MAX(u.timestamp) FROM messages u
                        WHERE u.session_id = m.session_id AND u.role = 'user' AND u.active = 1
                          AND u.display_kind IS NULL)
                FROM messages m JOIN (
                    SELECT session_id, MAX(id) AS mid FROM messages
                    WHERE active = 1 AND role IN ('user', 'assistant')
                      AND (display_kind IS NULL OR display_kind = 'failed_turn')
                      AND session_id IN (SELECT id FROM recent)
                    GROUP BY session_id
                ) l ON l.mid = m.id
                """,
                (limit,),
            ).fetchall()
        except sqlite3.Error:
            rows = []
        finally:
            con.close()
        per = out.setdefault(profile, {})
        for sid, role, kind, finish, content, ts, sent_at in rows:
            entry: Dict[str, Any] = {"sent_at": sent_at}
            if role == "assistant":
                entry["outcome"] = _classify_outcome(kind, finish, content)
                entry["at"] = ts
                if entry["outcome"] == "limited":
                    entry["limit_reset_at"] = _parse_limit_reset(content, int(ts * 1000) if isinstance(ts, (int, float)) else None)
            per[sid] = entry
    return out



@router.get("/outcomes")
async def get_outcomes(limit: int = Query(400, ge=1, le=10000)) -> Dict[str, Any]:
    return await asyncio.to_thread(_outcomes, limit)


# ─── interrupted turns (crash markers) ──────────────────────────────────────
# Hermes writes ``<home>/desktop/interrupted_turns.json`` when a turn starts and
# clears it when the turn concludes, so a marker whose writer process is gone is
# proof the app or backend died mid-turn. ``session.resume`` re-submits a fresh
# one, but only once somebody opens the session; the inbox asks for these to
# resume them on launch instead.
def _pid_dead(entry: Dict[str, Any]) -> Optional[bool]:
    pid = entry.get("writer_pid")
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return None
    try:
        from hermes_cli.active_sessions import _pid_liveness  # type: ignore
        live = _pid_liveness(pid, entry.get("writer_start_time"))
        return None if live is None else not live
    except Exception:
        pass
    try:
        os.kill(pid, 0)
        return False
    except ProcessLookupError:
        return True
    except OSError:
        return None


def _interrupted() -> Dict[str, Dict[str, Dict[str, Any]]]:
    """``{profile: {session_key: {started_at, attempts, auto_continue}}}`` for turns a dead writer left behind."""
    out: Dict[str, Dict[str, Dict[str, Any]]] = {}
    now = time.time()
    for profile, db in _profile_dbs():
        try:
            data = json.loads((db.parent / "desktop" / "interrupted_turns.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict):
            continue
        for key, entry in data.items():
            if not isinstance(entry, dict) or not str(entry.get("prompt") or "").strip():
                continue
            started = entry.get("started_at")
            if not isinstance(started, (int, float)) or now - started > 24 * 3600 or _pid_dead(entry) is not True:
                continue
            out.setdefault(profile, {})[key] = {
                "started_at": started,
                "attempts": _int(entry.get("attempts")),
                "auto_continue": bool(entry.get("auto_continue", True)),
            }
    return out


@router.get("/interrupted")
async def get_interrupted() -> Dict[str, Any]:
    return await asyncio.to_thread(_interrupted)


# ─── shared lifecycle (settle / snooze / Woke acknowledgement) ──────────────
# Inbox lifecycle used to live only in one Desktop window's plugin storage.
# Persisting it next to the sessions makes every client of this backend agree.
# Records are keyed ``<profile>|<durable session id>``; each carries ``t`` (ms
# of its last write) and the newer write wins, field set and all, so a cleared
# snooze travels as a record without ``snoozeUntil``.
_LIFECYCLE_MAX_RECORDS = 20000
_lifecycle_lock = threading.Lock()


def _lifecycle_path() -> Path:
    # Data dir keeps its pre-merge name so existing records carry over.
    return hermes_root() / "plugin-data" / "t3-inbox" / "lifecycle.json"


def _read_lifecycle() -> Dict[str, Dict[str, Any]]:
    try:
        data = json.loads(_lifecycle_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    records = data.get("records") if isinstance(data, dict) else None
    return {k: v for k, v in records.items() if isinstance(k, str) and isinstance(v, dict)} if isinstance(records, dict) else {}


def _write_lifecycle(records: Dict[str, Dict[str, Any]]) -> None:
    path = _lifecycle_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    if len(records) > _LIFECYCLE_MAX_RECORDS:
        keep = sorted(records.items(), key=lambda kv: kv[1].get("t") or 0, reverse=True)[:_LIFECYCLE_MAX_RECORDS]
        records = dict(keep)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps({"version": 1, "records": records}, separators=(",", ":")), encoding="utf-8")
    os.replace(tmp, path)


def _merge_lifecycle(incoming: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    with _lifecycle_lock:
        records = _read_lifecycle()
        changed = False
        for key, rec in incoming.items():
            if not isinstance(key, str) or "|" not in key or not isinstance(rec, dict):
                continue
            t = rec.get("t")
            if not isinstance(t, (int, float)):
                continue
            current = records.get(key)
            if current is None or (current.get("t") or 0) < t:
                records[key] = rec
                changed = True
        if changed:
            _write_lifecycle(records)
        return records


@router.get("/lifecycle")
async def get_lifecycle() -> Dict[str, Any]:
    return {"records": await asyncio.to_thread(_read_lifecycle)}


@router.post("/lifecycle")
async def post_lifecycle(body: Dict[str, Any]) -> Dict[str, Any]:
    incoming = body.get("records") if isinstance(body, dict) else None
    if not isinstance(incoming, dict):
        return {"records": await asyncio.to_thread(_read_lifecycle)}
    return {"records": await asyncio.to_thread(_merge_lifecycle, incoming)}
