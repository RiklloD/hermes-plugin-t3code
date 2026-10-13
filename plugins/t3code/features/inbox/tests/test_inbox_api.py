"""Backend tests for the Inbox feature's API: outcomes, crash markers and shared lifecycle.

Run: <python with pytest + Hermes deps> -m pytest plugins/t3code/features/inbox -q
"""

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path

import pytest

from conftest import load_feature_file


@pytest.fixture()
def api(hermes_home):
    mod = load_feature_file("inbox", "api.py", "t3code_inbox_api_test")
    mod._root = hermes_home
    return mod


def _db(path: Path, messages):
    con = sqlite3.connect(path)
    con.executescript(
        """
        CREATE TABLE sessions (id TEXT PRIMARY KEY, started_at REAL, last_activity_at REAL);
        CREATE TABLE messages (id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT, role TEXT,
            content TEXT, timestamp REAL, finish_reason TEXT, display_kind TEXT, active INTEGER DEFAULT 1);
        """
    )
    for sid in {m[0] for m in messages}:
        con.execute("INSERT INTO sessions VALUES (?, 0, 100)", (sid,))
    for sid, role, content, ts, finish, kind in messages:
        con.execute(
            "INSERT INTO messages (session_id, role, content, timestamp, finish_reason, display_kind) VALUES (?,?,?,?,?,?)",
            (sid, role, content, ts, finish, kind),
        )
    con.commit()
    con.close()


def test_outcomes_classify_and_timestamp(api):
    _db(api._root / "state.db", [
        ("ok", "user", "hi", 10, None, None), ("ok", "assistant", "done", 20, "stop", None),
        ("fail", "user", "go", 30, None, None),
        ("fail", "assistant", "Your request was not processed. Send it again if you still want me to carry it out.", 40, None, "failed_turn"),
        ("stop", "user", "go", 50, None, None), ("stop", "assistant", "Operation interrupted.", 60, None, None),
        ("lim", "user", "go", 70, None, None), ("lim", "assistant", "Provider said: usage limit reached", 80, "error", None),
        ("pending", "user", "first", 90, None, None), ("pending", "assistant", "ok", 95, "stop", None),
        ("pending", "user", "second", 99, None, None),
    ])
    out = api._outcomes(400)["default"]
    assert out["ok"] == {"outcome": "ok", "at": 20, "sent_at": 10}
    assert out["fail"]["outcome"] == "failed"
    assert out["stop"]["outcome"] == "stopped"
    assert out["lim"]["outcome"] == "limited"
    # A trailing user message: no outcome yet, but the send time is known.
    assert out["pending"] == {"sent_at": 99}


def test_lifecycle_newest_write_wins_and_persists(api):
    first = asyncio.run(api.post_lifecycle({"records": {"default|a": {"t": 10, "settledAt": 5}}}))
    assert first["records"]["default|a"]["settledAt"] == 5
    # Older write loses; newer write replaces the whole record (a cleared field stays cleared).
    asyncio.run(api.post_lifecycle({"records": {"default|a": {"t": 5, "snoozeUntil": 1}}}))
    asyncio.run(api.post_lifecycle({"records": {"default|a": {"t": 20, "visitedAt": 7}}}))
    got = asyncio.run(api.get_lifecycle())["records"]
    assert got == {"default|a": {"t": 20, "visitedAt": 7}}
    assert (api._root / "plugin-data" / "t3-inbox" / "lifecycle.json").is_file()


def test_lifecycle_rejects_malformed_records(api):
    asyncio.run(api.post_lifecycle({"records": {"nokey": {"t": 1}, "default|b": {"no": "t"}, "default|c": "x"}}))
    assert asyncio.run(api.get_lifecycle())["records"] == {}
    assert asyncio.run(api.post_lifecycle({"records": None}))["records"] == {}


def test_limit_reset_parsing(api):
    now = 1_760_000_000_000
    hour = 3_600_000
    assert api._parse_limit_reset("Claude AI usage limit reached|1760277600", now) == 1_760_277_600_000
    assert api._parse_limit_reset("try again in 4 days 2 hours 13 minutes", now) == now + 98 * hour + 13 * 60_000
    assert api._parse_limit_reset("try again in 2 hours 30 minutes", now) == now + 2 * hour + 30 * 60_000
    assert api._parse_limit_reset("rate limit, retry after 30 seconds", now) == now + 30_000
    assert api._parse_limit_reset("resets 2026-10-13T15:00:00Z", now) == 1_791_903_600_000
    assert api._parse_limit_reset("limit reached, resets at 3pm (Europe/Paris)", now) > now
    assert api._parse_limit_reset("Too many requests", now) is None


def test_outcomes_carry_limit_reset(api):
    db = api._root / "state.db"
    con = sqlite3.connect(db)
    con.executescript(
        """
        CREATE TABLE sessions (id TEXT, started_at REAL, last_activity_at REAL);
        CREATE TABLE messages (id INTEGER PRIMARY KEY, session_id TEXT, role TEXT, display_kind TEXT,
            finish_reason TEXT, content TEXT, timestamp REAL, active INTEGER DEFAULT 1);
        CREATE TABLE state_meta (key TEXT PRIMARY KEY, value TEXT);
        INSERT INTO sessions VALUES ('s', 1, 2);
        INSERT INTO messages (session_id, role, display_kind, finish_reason, content, timestamp)
            VALUES ('s', 'assistant', 'failed_turn', 'error', 'Usage limit reached, try again in 2 hours', 1760000000);
        """
    )
    con.commit()
    con.close()
    entry = api._outcomes(10)["default"]["s"]
    assert entry["outcome"] == "limited"
    assert entry["limit_reset_at"] == 1_760_000_000_000 + 2 * 3_600_000


def test_interrupted_reports_only_dead_writers(api, monkeypatch):
    import json
    import time

    (api._root / "state.db").write_bytes(b"")
    marks = api._root / "desktop"
    marks.mkdir()
    now = time.time()
    (marks / "interrupted_turns.json").write_text(json.dumps({
        "dead": {"prompt": "p", "started_at": now - 5, "writer_pid": 111, "attempts": 1},
        "alive": {"prompt": "p", "started_at": now - 5, "writer_pid": 222},
        "old": {"prompt": "p", "started_at": now - 90_000, "writer_pid": 111},
        "empty": {"prompt": " ", "started_at": now, "writer_pid": 111},
    }))
    monkeypatch.setattr(api, "_pid_dead", lambda e: e["writer_pid"] == 111)
    assert api._interrupted() == {"default": {"dead": {"started_at": now - 5, "attempts": 1, "auto_continue": True}}}
