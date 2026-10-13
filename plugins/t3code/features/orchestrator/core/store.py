"""Durable orchestration state (one SQLite file shared by every profile and process).

The engine in ``hermes serve`` owns side effects; other processes (the messaging
gateway's ``/t3`` command, the web dashboard) only read and edit rows, so every
write is a short transaction and rows carry the profile they belong to.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Iterable, Optional

TERMINAL = frozenset({"completed", "failed", "cancelled", "interrupted"})

_SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
  id TEXT PRIMARY KEY,
  parent_key TEXT NOT NULL,
  profile TEXT,
  parent_profile TEXT,
  root_key TEXT,
  depth INTEGER NOT NULL DEFAULT 1,
  child_key TEXT,
  child_title TEXT,
  title TEXT NOT NULL,
  role TEXT NOT NULL DEFAULT 'general',
  provider TEXT,
  model TEXT,
  cwd TEXT,
  prompt TEXT NOT NULL,
  status TEXT NOT NULL,
  result TEXT,
  error TEXT,
  progress TEXT,
  tool_count INTEGER NOT NULL DEFAULT 0,
  created_at REAL NOT NULL,
  started_at REAL,
  finished_at REAL,
  client_request_id TEXT,
  delivery TEXT NOT NULL DEFAULT 'none',
  delivered_at REAL,
  cancel_requested INTEGER NOT NULL DEFAULT 0,
  limit_reset_at REAL
);
CREATE UNIQUE INDEX IF NOT EXISTS tasks_request ON tasks(parent_key, client_request_id)
  WHERE client_request_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS tasks_parent ON tasks(parent_key);
CREATE INDEX IF NOT EXISTS tasks_child ON tasks(child_key);

CREATE TABLE IF NOT EXISTS queue (
  id TEXT PRIMARY KEY,
  session_key TEXT NOT NULL,
  profile TEXT,
  position REAL NOT NULL,
  text TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending',
  error TEXT,
  created_by TEXT NOT NULL DEFAULT 'user',
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL,
  sent_at REAL
);
CREATE INDEX IF NOT EXISTS queue_session ON queue(session_key, status, position);

CREATE TABLE IF NOT EXISTS holds (
  session_key TEXT PRIMARY KEY,
  reason TEXT NOT NULL,
  created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS transfers (
  id TEXT PRIMARY KEY,
  type TEXT NOT NULL,
  source_key TEXT NOT NULL,
  target_key TEXT,
  profile TEXT,
  source_point TEXT,
  base_point TEXT,
  source_provider TEXT,
  target_provider TEXT,
  strategy TEXT,
  status TEXT NOT NULL,
  delivery TEXT NOT NULL DEFAULT 'next_message',
  handoff TEXT,
  created_by TEXT NOT NULL DEFAULT 'user',
  created_at REAL NOT NULL,
  consumed_at REAL
);
CREATE INDEX IF NOT EXISTS transfers_target ON transfers(target_key, status);
CREATE INDEX IF NOT EXISTS transfers_source ON transfers(source_key);

CREATE TABLE IF NOT EXISTS recoveries (
  id TEXT PRIMARY KEY,
  session_key TEXT NOT NULL,
  profile TEXT,
  kind TEXT NOT NULL,
  reset_at REAL,
  status TEXT NOT NULL,
  detail TEXT,
  attempts INTEGER NOT NULL DEFAULT 0,
  created_at REAL NOT NULL,
  fired_at REAL
);
CREATE UNIQUE INDEX IF NOT EXISTS recoveries_identity ON recoveries(session_key, kind, reset_at);

CREATE TABLE IF NOT EXISTS schedules (
  id TEXT PRIMARY KEY,
  title TEXT NOT NULL,
  prompt TEXT NOT NULL,
  schedule TEXT NOT NULL,
  bind_key TEXT,
  profile TEXT,
  provider TEXT,
  model TEXT,
  cwd TEXT,
  enabled INTEGER NOT NULL DEFAULT 1,
  next_run_at REAL,
  last_run_at REAL,
  last_status TEXT,
  last_session_key TEXT,
  run_count INTEGER NOT NULL DEFAULT 0,
  webhook_token TEXT,
  signature TEXT,
  client_request_id TEXT UNIQUE,
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS turns (
  session_key TEXT PRIMARY KEY,
  profile TEXT,
  status TEXT NOT NULL,
  started_at REAL,
  ended_at REAL,
  last_user_at REAL,
  error TEXT,
  provider TEXT,
  model TEXT
);

CREATE TABLE IF NOT EXISTS settings (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS log (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts REAL NOT NULL,
  kind TEXT NOT NULL,
  ref TEXT,
  detail TEXT
);
"""

DEFAULT_SETTINGS: dict[str, Any] = {
    # Proactively resume turns a backend restart cut short (core only does it when a chat is reopened).
    "continue_after_restart": True,
    # Send "Continue where you left off." once a provider's usage limit resets.
    "resume_on_usage_limit": True,
    # Seconds after the reported reset before resuming (providers lift limits a little late).
    "usage_limit_grace_s": 60,
    # Depth limit for agents delegating to agents.
    "max_depth": 3,
    # Concurrent running delegated tasks per parent session.
    "max_running_per_parent": 8,
    # Loopback port for webhook schedules (0 disables the listener).
    "webhook_port": 8767,
    # Let a thread in one profile delegate to another profile (it runs as that profile).
    "cross_profile_delegation": True,
    # Byte cap for portable context handoffs (T3 default is 64 KB).
    "handoff_bytes": 48_000,
    # Let one thread switch between a Hermes-loop model and a native runtime (Claude CLI) with a context handoff.
    "harness_switch": True,
    # Byte budget of the handoff when one thread switches harness (e.g. a Hermes model -> Claude CLI).
    "harness_handoff_bytes": 160_000,
}


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def default_root() -> Path:
    try:
        from hermes_constants import get_default_hermes_root
        return Path(get_default_hermes_root())
    except Exception:
        return Path(os.environ.get("HERMES_ROOT") or Path.home() / ".hermes")


class Store:
    def __init__(self, path: Optional[Path] = None) -> None:
        self.path = Path(path or os.environ.get("T3O_DB") or default_root() / "plugin-data" / "t3-orchestrator" / "orchestrator.db")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        self._init_lock = threading.Lock()
        with self._init_lock:
            con = self._con()
            con.executescript(_SCHEMA)
            self._migrate(con)
            con.commit()

    _COLUMNS = {"tasks": {"child_title": "TEXT", "parent_profile": "TEXT"}, "recoveries": {"attempts": "INTEGER NOT NULL DEFAULT 0"}}

    def _migrate(self, con: sqlite3.Connection) -> None:
        for table, cols in self._COLUMNS.items():
            have = {r[1] for r in con.execute(f"PRAGMA table_info({table})").fetchall()}
            for col, decl in cols.items():
                if col not in have:
                    con.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")

    # ── plumbing ────────────────────────────────────────────────────────────
    def _con(self) -> sqlite3.Connection:
        con = getattr(self._local, "con", None)
        if con is None:
            con = sqlite3.connect(str(self.path), timeout=15, isolation_level=None, check_same_thread=False)
            con.row_factory = sqlite3.Row
            con.execute("PRAGMA journal_mode=WAL")
            con.execute("PRAGMA busy_timeout=15000")
            con.execute("PRAGMA foreign_keys=ON")
            self._local.con = con
        return con

    def execute(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Cursor:
        return self._con().execute(sql, tuple(params))

    def rows(self, sql: str, params: Iterable[Any] = ()) -> list[dict]:
        return [dict(r) for r in self.execute(sql, params).fetchall()]

    def row(self, sql: str, params: Iterable[Any] = ()) -> Optional[dict]:
        r = self.execute(sql, params).fetchone()
        return dict(r) if r is not None else None

    def insert(self, table: str, values: dict) -> None:
        cols = ", ".join(values)
        marks = ", ".join("?" for _ in values)
        self.execute(f"INSERT INTO {table} ({cols}) VALUES ({marks})", values.values())

    def update(self, table: str, key: str, values: dict, *, where: str = "", where_params: Iterable[Any] = ()) -> int:
        if not values:
            return 0
        sets = ", ".join(f"{c} = ?" for c in values)
        sql = f"UPDATE {table} SET {sets} WHERE id = ?" + (f" AND {where}" if where else "")
        return self.execute(sql, [*values.values(), key, *where_params]).rowcount

    def transaction(self):
        """``with store.transaction():`` — one IMMEDIATE transaction on this thread's connection."""
        store = self

        class _Tx:
            def __enter__(self_inner):
                store.execute("BEGIN IMMEDIATE")
                return store

            def __exit__(self_inner, exc_type, exc, tb):
                store.execute("ROLLBACK" if exc_type else "COMMIT")
                return False

        return _Tx()

    def log(self, kind: str, ref: str = "", detail: Any = None) -> None:
        text = detail if isinstance(detail, str) or detail is None else json.dumps(detail, default=str)[:4000]
        self.execute("INSERT INTO log (ts, kind, ref, detail) VALUES (?, ?, ?, ?)", (time.time(), kind, ref, text))
        self.execute("DELETE FROM log WHERE id < (SELECT MAX(id) - 2000 FROM log)")

    # ── settings ────────────────────────────────────────────────────────────
    def settings(self) -> dict:
        out = dict(DEFAULT_SETTINGS)
        for r in self.rows("SELECT key, value FROM settings"):
            try:
                out[r["key"]] = json.loads(r["value"])
            except ValueError:
                continue
        return out

    def setting(self, key: str) -> Any:
        return self.settings().get(key, DEFAULT_SETTINGS.get(key))

    def set_settings(self, patch: dict) -> dict:
        for key, value in patch.items():
            if key not in DEFAULT_SETTINGS:
                raise ValueError(f"unknown setting: {key}")
            default = DEFAULT_SETTINGS[key]
            if isinstance(default, bool):
                value = bool(value)
            elif isinstance(default, int):
                value = int(value)
            self.execute("INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                          (key, json.dumps(value)))
        return self.settings()

    # ── tasks ───────────────────────────────────────────────────────────────
    def task(self, task_id: str) -> Optional[dict]:
        return self.row("SELECT * FROM tasks WHERE id = ?", (task_id,))

    def task_by_request(self, parent_key: str, client_request_id: str) -> Optional[dict]:
        return self.row("SELECT * FROM tasks WHERE parent_key = ? AND client_request_id = ?", (parent_key, client_request_id))

    def task_for_child(self, child_key: str) -> Optional[dict]:
        return self.row("SELECT * FROM tasks WHERE child_key = ? ORDER BY created_at DESC LIMIT 1", (child_key,))

    def tasks_for_parent(self, parent_key: str, *, active_only: bool = False) -> list[dict]:
        sql = "SELECT * FROM tasks WHERE parent_key = ?"
        if active_only:
            sql += " AND status NOT IN ('completed','failed','cancelled','interrupted')"
        return self.rows(sql + " ORDER BY created_at", (parent_key,))

    def active_tasks(self) -> list[dict]:
        return self.rows("SELECT * FROM tasks WHERE status NOT IN ('completed','failed','cancelled','interrupted') ORDER BY created_at")

    def recent_tasks(self, limit: int = 200) -> list[dict]:
        return self.rows("SELECT * FROM tasks ORDER BY created_at DESC LIMIT ?", (limit,))

    def pending_deliveries(self) -> list[dict]:
        return self.rows("SELECT * FROM tasks WHERE delivery = 'pending' ORDER BY finished_at")

    # ── queue ───────────────────────────────────────────────────────────────
    def queue(self, session_key: str, *, include_done: bool = False) -> list[dict]:
        sql = "SELECT * FROM queue WHERE session_key = ?"
        if not include_done:
            sql += " AND status IN ('pending','sending')"
        return self.rows(sql + " ORDER BY position, created_at", (session_key,))

    def queued_sessions(self) -> list[str]:
        return [r["session_key"] for r in self.rows("SELECT DISTINCT session_key FROM queue WHERE status = 'pending'")]

    def hold(self, session_key: str) -> Optional[dict]:
        return self.row("SELECT * FROM holds WHERE session_key = ?", (session_key,))

    def set_hold(self, session_key: str, reason: Optional[str]) -> None:
        if reason:
            self.execute("INSERT INTO holds (session_key, reason, created_at) VALUES (?, ?, ?) "
                         "ON CONFLICT(session_key) DO UPDATE SET reason = excluded.reason, created_at = excluded.created_at",
                         (session_key, reason, time.time()))
        else:
            self.execute("DELETE FROM holds WHERE session_key = ?", (session_key,))

    # ── transfers ───────────────────────────────────────────────────────────
    def transfer(self, transfer_id: str) -> Optional[dict]:
        return self.row("SELECT * FROM transfers WHERE id = ?", (transfer_id,))

    def pending_transfers_for(self, target_key: str) -> list[dict]:
        return self.rows("SELECT * FROM transfers WHERE target_key = ? AND status = 'pending' ORDER BY created_at", (target_key,))

    def fork_origin(self, fork_key: str) -> Optional[dict]:
        return self.row("SELECT * FROM transfers WHERE type = 'fork' AND target_key = ? ORDER BY created_at DESC LIMIT 1", (fork_key,))

    def lineage(self, key: str) -> dict:
        return {
            "forked_from": self.fork_origin(key),
            "forks": self.rows("SELECT * FROM transfers WHERE type = 'fork' AND source_key = ? ORDER BY created_at", (key,)),
            "merges_in": self.rows("SELECT * FROM transfers WHERE type = 'merge_back' AND target_key = ? ORDER BY created_at", (key,)),
            "merges_out": self.rows("SELECT * FROM transfers WHERE type = 'merge_back' AND source_key = ? ORDER BY created_at", (key,)),
        }

    # ── turns (per-session run tracking) ────────────────────────────────────
    def turn(self, session_key: str) -> Optional[dict]:
        return self.row("SELECT * FROM turns WHERE session_key = ?", (session_key,))

    def upsert_turn(self, session_key: str, **values: Any) -> None:
        existing = self.turn(session_key)
        if existing is None:
            values.setdefault("status", "idle")
            self.insert("turns", {"session_key": session_key, **values})
        else:
            sets = ", ".join(f"{c} = ?" for c in values)
            self.execute(f"UPDATE turns SET {sets} WHERE session_key = ?", [*values.values(), session_key])


def loads(value: Any, default: Any = None) -> Any:
    if value in (None, ""):
        return default
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default


def dumps(value: Any) -> Optional[str]:
    return None if value is None else json.dumps(value, default=str)
