"""Muse Code + Antigravity usage readers (T3 Code's antigravityUsageReader / UsageLimits ports)."""
import json
import sqlite3
import time

from conftest import load_feature_file

native = load_feature_file("usage", "native.py", "t3code_usage_native_test")


def _varint(n):
    out = bytearray()
    while True:
        b = n & 127
        n >>= 7
        out.append(b | (128 if n else 0))
        if not n:
            return bytes(out)


def _field(number, value):
    if isinstance(value, int):
        return _varint(number << 3) + _varint(value)
    return _varint(number << 3 | 2) + _varint(len(value)) + value


def test_antigravity_generation_metadata_decodes_tokens_model_and_time(tmp_path, monkeypatch):
    usage = _field(1, 1018) + _field(2, 100) + _field(5, 40) + _field(3, 30) + _field(9, 10) + _field(11, b"resp-1")
    when = _field(4, _field(1, 1_700_000_000))
    gen = _field(1, _field(4, usage) + _field(9, when))
    db = tmp_path / "conversations" / "c1.db"
    db.parent.mkdir()
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE gen_metadata (idx INTEGER, data BLOB)")
    con.execute("INSERT INTO gen_metadata VALUES (0, ?)", (gen,))
    con.commit()
    con.close()
    monkeypatch.setenv("ANTIGRAVITY_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(native, "profile_dirs", lambda: [])
    monkeypatch.setattr(native, "HOME", tmp_path)
    records = native.antigravity_records(0)
    assert records == [(1_700_000_000_000, "gemini-3-flash-preview", 100, 40, 0, 30, 10, "standard",
                        "antigravity:11:resp-1", "c1")]


def test_muse_journal_model_calls_become_records(tmp_path, monkeypatch):
    session = tmp_path / "sessions" / "2026" / "s1"
    session.mkdir(parents=True)
    rows = [{"id": "r1", "recorded_at": 1_791_886_953_847_245, "stream": {"id": "s1"},
             "payload": {"event": {"kind": "model_completed", "model": "muse-spark-1.3", "usage": {
                 "input_tokens": 1000, "cache_read_tokens": 800, "output_tokens": 50, "reasoning_tokens": 20}}}},
            {"id": "r2", "payload": {"event": {"kind": "goal_usage_attribution"}}}]
    (session / "session.jsonl").write_text("\n".join(json.dumps(r) for r in rows))
    monkeypatch.setenv("MUSE_HOME", str(tmp_path))
    assert native.muse_records(0) == [(1_791_886_953_847, "muse-spark-1.3", 200, 800, 0, 50, 20, "standard",
                                       "muse:r1", "s1")]


def test_muse_limits_use_the_last_observation_and_expire_passed_windows(hermes_home):
    path = hermes_home / "plugin-data" / "t3code" / "muse-usage.json"
    path.parent.mkdir(parents=True)
    now = time.time() * 1000
    path.write_text(json.dumps({"window": {"usedPercent": 40, "windowDurationMins": 300, "resetsAtMs": now - 1000},
                                "weekly": {"usedPercent": 7, "resetsAtMs": now + 86_400_000}, "observedAtMs": now}))
    win = lambda i, l, k, u, r, m: {"id": i, "kind": k, "usedPercent": u, "resetsAt": r}
    out = native.muse_limits(win)
    assert out["status"] == "ok"
    assert out["windows"][0]["usedPercent"] == 0.0 and out["windows"][0]["idle"]  # the 5h window already reset
    assert out["windows"][1]["usedPercent"] == 7


def test_antigravity_quota_summary_maps_shared_buckets():
    win = lambda i, l, k, u, r, m: {"id": i, "label": l, "kind": k, "usedPercent": u, "mins": m}
    rows = native._summary_windows(win, {"groups": [{"buckets": [
        {"bucketId": "gemini-5h", "window": "5h", "remainingFraction": 0.75},
        {"bucketId": "3p-weekly", "window": "weekly", "remainingFraction": 1.0},
        {"bucketId": "x", "disabled": True, "remainingFraction": 0}]}]})
    assert rows == [{"id": "gemini-5h", "label": "Gemini · 5-hour", "kind": "session", "usedPercent": 25.0, "mins": 300},
                    {"id": "3p-weekly", "label": "Claude & GPT (shared) · Weekly", "kind": "weekly", "usedPercent": 0.0, "mins": 10080}]
