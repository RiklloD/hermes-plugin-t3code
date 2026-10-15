"""Restart continuation policy (T3 Code ``RestartContinuation.ts`` + ``RestartBackgroundNote.ts``).

A backend restart (update, quit, crash) cuts every running turn. T3 records the cut runs and, once
the server is back, prompts each one with "Continue where you left off.", carrying a bounded note
of the background work the restart killed. Pure helpers here; the engine owns the I/O:

* intent is captured twice: a per-process snapshot of live work refreshed while turns run (so a
  crash leaves it behind), and a synchronous capture right before a graceful shutdown interrupts
  the turns (``prepare_for_shutdown``: an interrupted turn clears core's crash marker);
* :func:`blocked` is checked when the continuation is delivered (T3 checks both ends);
* :data:`MAX_CONTINUATION_CHAIN` stops a turn that keeps killing the backend from restarting forever.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import time
from pathlib import Path
from typing import Any, Iterable, Optional

CONTINUE_PROMPT = "Continue where you left off."
# Consecutive automatic continuations allowed before a person has to step in.
MAX_CONTINUATION_CHAIN = 3
MAX_NOTE_ENTRIES = 10
MAX_LABEL_LENGTH = 160
# Intent older than this is history, not a turn to resume.
MAX_INTENT_AGE_S = 6 * 3600
SUMMARY_RESUMED = "T3 Code restarted and resumed this turn"
SUMMARY_STOPPED = "T3 Code restarted and stopped background work"


def compact_label(value: Any) -> Optional[str]:
    if not isinstance(value, str):
        return None
    text = re.sub(r"\s+", " ", value).strip()
    if not text:
        return None
    return text if len(text) <= MAX_LABEL_LENGTH else text[: MAX_LABEL_LENGTH - 1] + "…"


def is_awaited(work: dict) -> bool:
    """Work an agent ends its turn to wait for (subagent, monitor, background task). A shell does not
    wake a settled turn: most are dev servers nobody waits on, so their note waits for the next message."""
    return work.get("kind") != "shell"


def merge_work(current: Iterable[dict], added: Iterable[dict]) -> list[dict]:
    identity = lambda w: w.get("id") or f"{w.get('kind')}\0{w.get('label')}"  # noqa: E731
    merged = list(current)
    seen = {identity(w) for w in merged}
    for w in added:
        if identity(w) not in seen:
            seen.add(identity(w))
            merged.append(w)
    return merged


def background_note(work: list[dict]) -> str:
    """Provider-facing text for work the model still expects to hear back from (bounded)."""
    omitted = len(work) - MAX_NOTE_ENTRIES
    lines = ["Note: the Hermes backend restarted, and this background work was cancelled before it finished. "
             "It will not report back:"]
    lines += [f"- {w.get('kind')}: {w.get('label')}" for w in work[:MAX_NOTE_ENTRIES]]
    if omitted > 0:
        lines.append(f"- and {omitted} more")
    return "\n".join(lines)


def continuation_text(work: list[dict], settled: bool) -> str:
    """A cut turn gets the note plus the prompt; a settled turn that lost awaited work gets the note alone."""
    if not work:
        return CONTINUE_PROMPT
    note = background_note(work)
    return note if settled else f"{note}\n\n{CONTINUE_PROMPT}"


def is_maintenance_command(prompt: Any) -> bool:
    """A slash command (``/compact``, ``/claude:compact`` …) is maintenance, not work to continue."""
    return isinstance(prompt, str) and prompt.lstrip().startswith("/")


# ── thread lifecycle (the Inbox feature's settle / snooze records) ─────────────

def lifecycle_path() -> Path:
    from .store import default_root
    return default_root() / "plugin-data" / "t3-inbox" / "lifecycle.json"


def lifecycle_record(profile_name: str, key: str, path: Optional[Path] = None) -> dict:
    """The Inbox record for ``<profile>|<durable id>``; empty when the Inbox never touched the thread."""
    with contextlib.suppress(OSError, ValueError, TypeError, AttributeError):
        records = json.loads((path or lifecycle_path()).read_text(encoding="utf-8")).get("records") or {}
        rec = records.get(f"{profile_name}|{key}")
        return rec if isinstance(rec, dict) else {}
    return {}


def blocked(*, lifecycle: dict, archived: bool, awaiting_user: bool, now: Optional[float] = None) -> Optional[str]:
    """Why a restart must leave this thread for the user (None = it may continue): put away
    (archived / settled / snoozed) or the user owes an answer (an approval, question or secret)."""
    now_ms = (now if now is not None else time.time()) * 1000
    if archived:
        return "archived"
    settled_at, unsettled_at = lifecycle.get("settledAt"), lifecycle.get("unsettledAt")
    if settled_at and not (unsettled_at and unsettled_at > settled_at):
        return "settled"
    if (lifecycle.get("snoozeUntil") or 0) > now_ms:
        return "snoozed"
    if awaiting_user:
        return "awaiting_user"
    return None


# ── process liveness (same answer core gives for its turn markers) ─────────────

def own_identity() -> tuple[int, Optional[float]]:
    start = None
    with contextlib.suppress(Exception):
        from hermes_cli.active_sessions import _own_start_time
        start = _own_start_time()
    return os.getpid(), start


def writer_dead(pid: Any, start: Any = None) -> bool:
    """True only when the process is known to be gone (a reused pid reads dead)."""
    if not isinstance(pid, int) or pid <= 0:
        return False
    if pid == os.getpid():
        return False
    with contextlib.suppress(Exception):
        from hermes_cli.active_sessions import _pid_liveness
        live = _pid_liveness(pid, start)
        if live is not None:
            return not live
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    except OSError:
        return False
    return False


def pid_alive(pid: Any) -> bool:
    if not isinstance(pid, int) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True
    return True
