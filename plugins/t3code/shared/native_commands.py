"""The native agents' own slash commands, kept ready for Hermes's slash menu.

Hermes asks a provider for its commands while the user types ``/`` (``agent.native_commands``), so the
answer must be quick. Each provider keeps its last list per key (workspace) here, in memory and in
``plugin-data/t3code/commands.json``; a stale list is answered at once and refreshed in the background.
A live session pushes the list its agent reported, which is always the freshest.

Rows: ``{"name", "description", "args_hint", "kind": "command" | "skill"}``.
"""

from __future__ import annotations

import contextvars
import json
import logging
import threading
import time
from typing import Callable, Iterable, Optional

from .hermes_data import hermes_root

logger = logging.getLogger("t3code.commands")

FRESH_SECONDS = 120
LATEST = "*"  # per provider: the newest list for any key, offered for a workspace never listed

_lock = threading.Lock()
_entries: dict[str, dict] = {}  # "<provider>|<key>" -> {"at": monotonic, "rows": [...]}
_refreshing: dict[str, threading.Event] = {}
_loaded = False


def _path():
    return hermes_root() / "plugin-data" / "t3code" / "commands.json"


def _load():  # under _lock
    global _loaded
    if _loaded:
        return
    _loaded = True
    try:
        data = json.loads(_path().read_text())
    except (OSError, ValueError):
        return
    for slot, entry in (data if isinstance(data, dict) else {}).items():
        if isinstance(entry, dict) and isinstance(entry.get("rows"), list):
            _entries.setdefault(slot, {"at": float("-inf"), "rows": entry["rows"]})  # stale until refreshed


def _save():  # under _lock
    try:
        path = _path()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps({slot: {"rows": e["rows"]} for slot, e in _entries.items()}))
        tmp.replace(path)
    except OSError:
        pass


def row(name, description="", args_hint="", kind="command") -> dict:
    return {"name": str(name), "description": " ".join(str(description or "").split()),
            "args_hint": str(args_hint or "").strip(), "kind": "skill" if kind == "skill" else "command"}


def put(provider: str, key: str, rows: Iterable[dict]) -> list[dict]:
    rows = [row(**{k: r.get(k) for k in ("name", "description", "args_hint", "kind") if r.get(k) is not None})
            for r in rows if r.get("name")]
    with _lock:
        _load()
        now = time.monotonic()
        _entries[f"{provider}|{key}"] = {"at": now, "rows": rows}
        _entries[f"{provider}|{LATEST}"] = {"at": now, "rows": rows}
        _save()
    return rows


def _refresh(provider: str, key: str, loader: Callable[[], Optional[list]]) -> threading.Event:
    slot = f"{provider}|{key}"
    with _lock:
        if (done := _refreshing.get(slot)) is not None:
            return done
        done = _refreshing[slot] = threading.Event()

    def run():
        try:
            rows = loader()
            if rows is not None:
                put(provider, key, rows)
        except Exception:
            logger.debug("refreshing %s commands failed", provider, exc_info=True)
        finally:
            with _lock:
                _refreshing.pop(slot, None)
            done.set()

    # The caller's context carries the Hermes profile (home) the loader must read settings from.
    context = contextvars.copy_context()
    threading.Thread(target=context.run, args=(run,), name=f"t3code-{provider}-commands", daemon=True).start()
    return done


def get(provider: str, key: str, loader: Optional[Callable[[], Optional[list]]] = None, *,
        wait: float = 0.0, default: Iterable[dict] = ()) -> list[dict]:
    """The rows for ``(provider, key)``. A stale or missing entry is refreshed with ``loader`` in the
    background; with no entry at all this waits up to ``wait`` seconds for it, then falls back to the
    provider's newest list for any key, then ``default``."""
    slot = f"{provider}|{key}"
    with _lock:
        _load()
        entry = _entries.get(slot)
    if loader is not None and (entry is None or time.monotonic() - entry["at"] > FRESH_SECONDS):
        done = _refresh(provider, key, loader)
        if entry is None and wait > 0 and done.wait(wait):
            with _lock:
                entry = _entries.get(slot)
    if entry is None:
        with _lock:
            entry = _entries.get(f"{provider}|{LATEST}")
    return list(entry["rows"]) if entry else [row(**r) for r in default]
