"""Read-only goal projection shared by every Hermes provider.

Hermes owns API/Muse/ACP goals; native Claude owns its Stop-hook goal. Read the
persisted sources, never infer a goal from ordinary prose or start a second loop.
"""
from __future__ import annotations

import json
import re

_COMMAND = re.compile(r"^/(?:claude:)?goal(?:\s+([\s\S]*))?$")
_ACTIVE = re.compile(r"^Goal active: ([\s\S]+?) \((?:not yet evaluated|(\d+) turns?)\)")


def native_goal(rows):
    """Recover confirmed native commands, including compacted display history.

    A model mentioning 'Goal set:' is not authority. An exact command receipt must
    follow a matching user /goal. Status queries can recover an older goal too.
    """
    goal, pending = None, None
    for row in rows:
        content = row["content"] or ""
        if row["display_kind"] == "hidden":
            continue
        text = content.strip()
        if row["role"] == "user":
            match = _COMMAND.fullmatch(text)
            pending = (match.group(1) or "").strip() if match else None
            continue
        if row["role"] != "assistant" or pending is None:
            continue
        objective = pending[4:].strip() if pending.startswith("set ") else pending
        if text.startswith("Goal set: ") and text[10:].strip() == objective and objective:
            goal = {"objective": objective, "status": "active", "source": "native",
                    "updated_at": row["timestamp"]}
        elif pending in ("clear", "off") and text.startswith("Goal cleared"):
            goal = {"objective": "", "status": "cleared", "source": "native",
                    "updated_at": row["timestamp"]}
        elif pending in ("", "status"):
            active = _ACTIVE.match(text)
            if active:
                goal = {"objective": active[1], "status": "active", "source": "native",
                        "checks": int(active[2] or 0), "updated_at": row["timestamp"]}
            elif text.startswith("No goal set"):
                goal = {"objective": "", "status": "cleared", "source": "native",
                        "updated_at": row["timestamp"]}
        # Only the first assistant response is the local command receipt.
        pending = None
    return goal


def hermes_goal(raw):
    if not raw:
        return None
    state = json.loads(raw)
    if not isinstance(state, dict):
        return None
    waiting = state.get("waiting_on_pid") or state.get("waiting_on_session") or state.get("waiting_until")
    status = state.get("status", "active")
    return {"objective": str(state.get("goal") or ""),
            "status": "waiting" if status == "active" and waiting else status,
            "source": "hermes", "checks": state.get("turns_used", 0),
            "reason": state.get("waiting_reason") or state.get("paused_reason") or state.get("last_reason"),
            "updated_at": max(float(state.get("created_at") or 0), float(state.get("last_turn_at") or 0))}


def read_goal(db, session_id):
    """Return one canonical display record; terminal tombstones beat older goals."""
    row = db.execute("SELECT id FROM sessions WHERE id = ?", (session_id,)).fetchone()
    if row is None:
        raise LookupError(session_id)
    stored = db.execute("SELECT value FROM state_meta WHERE key = ?", (f"goal:{session_id}",)).fetchone()
    core = hermes_goal(stored[0]) if stored else None
    # No LIMIT: old active goals must survive arbitrarily long chats/compaction.
    # SQL selects only potential command/receipt rows, not the entire transcript.
    rows = db.execute("""SELECT role, content, display_kind, timestamp
        FROM messages WHERE session_id = ? AND active = 1 AND role IN ('user', 'assistant')
        AND (role = 'user' OR content LIKE 'Goal %' OR content LIKE 'No goal set%') ORDER BY id""", (session_id,))
    native = native_goal(rows)
    candidates = [goal for goal in (core, native) if goal is not None]
    goal = max(candidates, key=lambda item: item["updated_at"]) if candidates else None
    return None if goal and goal["status"] == "cleared" else goal
