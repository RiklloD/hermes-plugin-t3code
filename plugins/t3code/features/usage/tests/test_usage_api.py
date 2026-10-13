"""Backend tests for the T3 Usage dashboard API: transcript parsing, dedupe,
aggregation and limit-window shaping on synthetic CLIs.

Run: <python with pytest + Hermes deps> -m pytest plugins/t3code/features/usage -q
"""

from __future__ import annotations

import importlib.util
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

API = Path(__file__).resolve().parents[1] / "api.py"


def _iso(seconds_ago: float = 0) -> str:
    return datetime.fromtimestamp(time.time() - seconds_ago, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _write(p: Path, records) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")


@pytest.fixture()
def api(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location("t3_usage_plugin_api_test", API)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    root = tmp_path / ".hermes"
    root.mkdir()
    monkeypatch.setattr(mod, "HOME", tmp_path)
    monkeypatch.setitem(sys.modules, "hermes_constants", type(sys)("hermes_constants"))
    sys.modules["hermes_constants"].get_default_hermes_root = lambda: str(root)
    # Synthetic transcript trees instead of the real ~/.claude / ~/.codex.
    claude, codex = tmp_path / "claude-projects", tmp_path / "codex-sessions"
    claude.mkdir()
    codex.mkdir()
    monkeypatch.setattr(mod, "CLAUDE_ROOTS", [claude])
    # Nor the real Antigravity / Muse Code history (covered by test_native_usage.py).
    monkeypatch.setattr(mod._native, "antigravity_records", lambda cutoff: [])
    monkeypatch.setattr(mod._native, "muse_records", lambda cutoff: [])
    monkeypatch.setattr(mod, "CODEX_ROOTS", [codex])
    monkeypatch.setattr(mod, "_price_cache", {})
    mod._file_cache.clear()
    return mod, claude, codex


def test_usage_claude_dedupe_and_totals(api):
    mod, claude, _codex = api
    usage = {"input_tokens": 100, "cache_read_input_tokens": 50,
             "cache_creation_input_tokens": 10, "output_tokens": 40}
    # Two content blocks of ONE assistant message (same id) count once;
    # a second message with a different id counts again.
    _write(claude / "p" / "s.jsonl", [
        {"type": "assistant", "timestamp": _iso(), "sessionId": "s1",
         "message": {"id": "m1", "model": "claude-sonnet-4-5", "usage": usage}},
        {"type": "assistant", "timestamp": _iso(), "sessionId": "s1",
         "message": {"id": "m1", "model": "claude-sonnet-4-5", "usage": usage}},
        {"type": "assistant", "timestamp": _iso(), "sessionId": "s1",
         "message": {"id": "m2", "model": "claude-sonnet-4-5", "usage": usage}},
        {"type": "user", "timestamp": _iso(), "message": {"role": "user"}},  # not assistant
    ])
    out = mod._compute(7)
    tok = 100 + 50 + 10 + 40
    assert out["providers"]["claude"]["tokens"] == 2 * tok
    assert out["providers"]["claude"]["sessions"] == 1
    assert out["totals"]["cached"] == 2 * 50
    assert out["totals"]["uncached"] == 2 * 100
    assert out["totals"]["output"] == 2 * 40
    assert len(out["series"]) == 7  # one bucket per day
    assert sum(p["tokens"] for p in out["providers"].values()) == out["totals"]["tokens"]


def test_usage_codex_rollout_repeats_suppressed(api):
    mod, _claude, codex = api
    last = {"input_tokens": 200, "cached_input_tokens": 80,
            "cache_write_input_tokens": 20, "output_tokens": 60, "reasoning_output_tokens": 30}
    _write(codex / "2026" / "10" / "r.jsonl", [
        {"type": "session_meta", "timestamp": _iso(), "payload": {"id": "cs1"}},
        {"type": "turn_context", "payload": {"model": "gpt-5.2-codex"}},
        {"timestamp": _iso(), "payload": {"type": "token_count", "info": {"last_token_usage": last}}},
        {"timestamp": _iso(), "payload": {"type": "token_count", "info": {"last_token_usage": last}}},
    ])
    out = mod._compute(1)
    tok = (200 - 80 - 20) + 80 + 20 + 60  # uncached + cached + cache-write + output
    assert out["providers"]["codex"]["tokens"] == tok
    assert out["providers"]["codex"]["via"] == {"cli": tok}
    assert out["providers"]["codex"]["sessions"] == 1
    assert out["resolution"] == "hour"
    assert len(out["series"]) == 24  # one bucket per hour


def test_window_clamps_and_helpers(api):
    mod, _c, _x = api
    w = mod._window("x", "Session", "session", 137.5, None, 300)
    assert w["usedPercent"] == 100.0 and w["resetsAt"] is None and w["windowDurationMins"] == 300
    assert mod._codex_speed("priority") == "fast"
    assert mod._codex_speed("ultrafast") == "ultrafast"
    assert mod._codex_speed("whatever") == "standard"
    assert mod._rates("claude", "<synthetic>") is None  # never priced
    assert mod._bucket_key(0, True).endswith(":00")  # hourly buckets are hour-aligned
