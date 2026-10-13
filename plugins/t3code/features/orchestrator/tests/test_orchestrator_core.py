"""Offline tests for the orchestration core (no gateway, no models).

Run: HERMES_HOME=$(mktemp -d) hermes-agent/scripts/run-in-hermes-env python3 -m pytest plugins/t3code/features/orchestrator -q
"""

import hashlib
import hmac
import importlib
import importlib.util
import sys
import time
from datetime import datetime
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def _core():
    if "t3o" not in sys.modules:
        spec = importlib.util.spec_from_file_location("t3o", ROOT / "core" / "__init__.py",
                                                      submodule_search_locations=[str(ROOT / "core")])
        mod = importlib.util.module_from_spec(spec)
        sys.modules["t3o"] = mod
        spec.loader.exec_module(mod)
    return sys.modules["t3o"]


_core()
sched = importlib.import_module("t3o.schedule")
handoff = importlib.import_module("t3o.handoff")
store_mod = importlib.import_module("t3o.store")
engine_mod = importlib.import_module("t3o.engine")


@pytest.fixture()
def eng(tmp_path):
    return engine_mod.Engine(store_mod.Store(tmp_path / "o.db"))


def test_schedule_normalize_rejects_short_interval():
    with pytest.raises(ValueError):
        sched.normalize({"type": "interval", "everyMs": 1000})
    assert sched.normalize('{"type":"interval","everyMs":60000}') == {"type": "interval", "everyMs": 60000}


def test_fixed_time_weekdays_use_sunday_zero():
    trig = sched.normalize({"type": "fixed_time", "timeOfDay": "9:05", "weekdays": [1]})
    assert trig["timeOfDay"] == "09:05"
    nxt = sched.next_run(trig, after=time.time())
    d = datetime.fromtimestamp(nxt)
    assert d.weekday() == 0 and (d.hour, d.minute) == (9, 5)  # Python Monday == T3 weekday 1


def test_interval_next_run_skips_missed_slots():
    trig = {"type": "interval", "everyMs": 60_000}
    now = 10_000.0
    assert sched.next_run(trig, after=now, last_run=now - 3600) > now


def test_signature_and_placeholders():
    body = b'{"tag":"v1"}'
    sig = {"header": "X-Sig", "encoding": "hex", "prefix": "sha256=", "secret": "k"}
    good = "sha256=" + hmac.new(b"k", body, hashlib.sha256).hexdigest()
    assert sched.verify_signature(sig, {"x-sig": good}, body)
    assert not sched.verify_signature(sig, {"x-sig": "sha256=00"}, body)
    req = {"body": {"tag": "v1", "repo": {"name": "r"}}, "headers": {"X-Actor": "ci", "Authorization": "secret"}, "query": {"a": "1"}}
    out = sched.render_prompt("{{body.tag}} {{body.repo.name}} {{headers.x-actor}} {{headers.authorization}} {{query.a}}", req)
    assert out == "v1 r ci  1"
    assert "secret" not in sched.render_prompt("{{request}}", req)


MSGS = [
    {"role": "user", "content": "one", "_row_id": 1},
    {"role": "assistant", "content": None, "tool_calls": [{"function": {"name": "terminal", "arguments": '{"command":"ls"}'}}]},
    {"role": "tool", "content": "a b"},
    {"role": "assistant", "content": "did one"},
    {"role": "user", "content": "two", "_row_id": 5},
    {"role": "assistant", "content": ""},
    {"role": "user", "content": "[System: hidden]", "display_kind": "hidden"},
    {"role": "user", "content": "three", "_row_id": 8},
    {"role": "assistant", "content": "did three"},
]


def test_runs_and_cuts_include_interrupted_runs():
    rs = handoff.runs(MSGS)
    assert [r["ordinal"] for r in rs] == [1, 2, 3]
    assert rs[0]["status"] == "completed" and rs[0]["tool_calls"] == 1
    assert rs[1]["status"] == "interrupted"
    assert handoff.cut_for_run(MSGS, 2) == 7
    assert handoff.cut_for_run(MSGS, None) == len(MSGS)
    with pytest.raises(ValueError):
        handoff.cut_for_run(MSGS, 9)


def test_portable_context_is_bounded_and_keeps_newest():
    big = [{"role": "user", "content": f"msg {i} " + "x" * 900} for i in range(200)]
    text = handoff.portable_context(big, title="t", source_key="k", source_provider="p", source_model="m",
                                    reason="fork", budget=8000)
    assert len(text.encode()) < 9000
    assert "msg 199" in text and "omitted" in text
    delta = handoff.delta_context(MSGS, fork_title="f", fork_key="fk", base_count=4, budget=8000)
    assert "two" in delta and "did one" not in delta and "terminal(ls)" not in delta


def test_queue_reorder_edit_remove(eng):
    key = "s1"
    a = eng.queue_add(key, "A", None)["queuedId"]
    b = eng.queue_add(key, "B", None)["queuedId"]
    c = eng.queue_add(key, "C", None)["queuedId"]
    eng.queue_reorder(key, c, a)
    assert [i["text"] for i in eng.queue_list(key)["items"]] == ["C", "A", "B"]
    eng.queue_reorder(key, c, None)
    assert [i["text"] for i in eng.queue_list(key)["items"]] == ["A", "B", "C"]
    eng.queue_edit(key, b, "B2")
    eng.queue_remove(key, a)
    assert [i["text"] for i in eng.queue_list(key)["items"]] == ["B2", "C"]
    with pytest.raises(engine_mod.OrchestratorError):
        eng.queue_edit(key, a, "again")


def test_next_message_transfers_are_consumed_once(eng):
    eng.store.insert("transfers", {"id": "x1", "type": "merge_back", "source_key": "f", "target_key": "s",
                                   "status": "pending", "delivery": "next_message", "handoff": "CTX",
                                   "created_at": time.time()})
    assert eng.consume_for_prompt("s") == "CTX"
    assert eng.consume_for_prompt("s") is None


def test_usage_limit_arming_is_idempotent(eng):
    reset = time.time() + 600
    assert eng._arm_usage_limit("s", None, reset, {"code": "usage_limit"})
    assert eng._arm_usage_limit("s", None, reset, {"code": "usage_limit"})
    assert len(eng.store.rows("SELECT * FROM recoveries")) == 1
    assert not eng._arm_usage_limit("s", None, time.time() - 3600, {})


def test_settings_validation(eng):
    assert eng.store.set_settings({"resume_on_usage_limit": 0})["resume_on_usage_limit"] is False
    with pytest.raises(ValueError):
        eng.store.set_settings({"nope": 1})


# ── regressions for the GPT-6.1-Sol review ─────────────────────────────────

def _task(eng, tid, parent, child, status="running", **kw):
    eng.store.insert("tasks", {"id": tid, "parent_key": parent, "root_key": parent, "depth": 1, "child_key": child,
                               "title": tid, "role": "general", "prompt": "x", "status": status,
                               "created_at": time.time(), "started_at": time.time(), "delivery": "none", **kw})


def test_stop_tree_reaches_grandchildren_through_finished_child(eng):
    _task(eng, "child", "P", "C", status="completed")
    _task(eng, "grand", "C", "G", status="running")
    marked = eng._mark_cancel_tree("P", "parent stopped")
    assert [t["id"] for t in marked] == ["grand"]
    assert eng.store.task("grand")["cancel_requested"] == 1
    assert eng.store.task("child")["status"] == "completed"


def test_finalize_is_exactly_once_and_cancel_is_not_resurrected(eng):
    _task(eng, "t1", "P", "C")
    eng._finalize(eng.store.task("t1"), {"status": "cancelled", "error": "stop"})
    eng._finalize(eng.store.task("t1"), {"status": "completed", "result": "late"})
    t = eng.store.task("t1")
    assert t["status"] == "cancelled" and t["result"] is None
    assert len(eng.store.rows("SELECT * FROM transfers WHERE type='subagent_result'")) == 1
    # a late usage-limit "waiting" transition must not revive a terminal task
    assert eng.store.update("tasks", "t1", {"status": "waiting"},
                            where="status NOT IN ('completed','failed','cancelled','interrupted') AND cancel_requested = 0") == 0


def test_child_with_running_grandchildren_waits(eng, monkeypatch):
    _task(eng, "child", "P", "C")
    _task(eng, "grand", "C", "G")
    monkeypatch.setattr(eng, "_last_reply", lambda t: "")
    eng._on_turn_complete("sid", "C", None, eng.store.task("child"), {"status": "complete", "text": "started a helper"})
    assert eng.store.task("child")["status"] == "waiting_children"
    eng._finalize(eng.store.task("grand"), {"status": "completed", "result": "done"})
    eng._on_turn_complete("sid", "C", None, eng.store.task("child"), {"status": "complete", "text": "final"})
    assert eng.store.task("child")["status"] == "completed" and eng.store.task("child")["result"] == "final"


def test_usage_limit_rearms_after_fire_and_is_bounded(eng):
    reset = time.time() + 600
    eng._arm_usage_limit("s", None, reset, {})
    rid = eng.store.rows("SELECT id FROM recoveries")[0]["id"]
    for attempt in range(2):  # resumes 1 and 2 re-arm; the third fired resume is the last
        eng.store.update("recoveries", rid, {"status": "fired", "attempts": attempt + 1})
        assert eng._arm_usage_limit("s", None, reset, {})
        assert eng.store.row("SELECT status FROM recoveries WHERE id = ?", (rid,))["status"] == "armed"
    eng.store.update("recoveries", rid, {"status": "fired", "attempts": 3})
    assert not eng._arm_usage_limit("s", None, reset, {})
    assert eng.store.row("SELECT status FROM recoveries WHERE id = ?", (rid,))["status"] == "exhausted"


def test_claimed_queue_item_cannot_be_edited(eng):
    qid = eng.queue_add("s", "old", None)["queuedId"]
    eng.store.execute("UPDATE queue SET status = 'sending' WHERE id = ?", (qid,))
    with pytest.raises(engine_mod.OrchestratorError):
        eng.queue_edit("s", qid, "new")


def test_tool_call_preamble_is_not_a_completed_run():
    msgs = [{"role": "user", "content": "do it"},
            {"role": "assistant", "content": "I will inspect it now", "tool_calls": [{"function": {"name": "terminal"}}]},
            {"role": "tool", "content": "..."}]
    assert handoff.runs(msgs)[0]["status"] == "interrupted"


def test_signed_webhooks_trust_only_the_body():
    req = {"body": {"a": "1"}, "headers": {"X-Evil": "rm -rf"}, "query": {"q": "inject"}}
    assert sched.render_prompt("{{body.a}}|{{headers.x-evil}}|{{query.q}}|{{request}}", req, signed=True) == "1|||"


def test_webhook_replay_is_ignored(eng, monkeypatch):
    runs = []
    monkeypatch.setattr(eng, "schedule_run", lambda sid, req=None: runs.append(sid) or {"ok": True})
    eng.store.insert("schedules", {"id": "w", "title": "w", "prompt": "p", "schedule": '{"type":"webhook"}',
                                   "enabled": 1, "webhook_token": "tok", "created_at": 1, "updated_at": 1})
    req = {"headers": {}, "body": {"x": 1}}
    assert eng._on_webhook("tok", req, b'{"x":1}')[0] == 202
    assert eng._on_webhook("tok", req, b'{"x":1}')[0] == 200
    assert runs == ["w"]


def test_target_profile_validation(eng, monkeypatch):
    monkeypatch.setattr(eng, "_profiles", lambda: [(None, Path("/r")), ("advisor", Path("/r/profiles/advisor"))])
    assert eng._target_profile({}, "default") == ("default", False)
    assert eng._target_profile({"profile": "default"}, None) == (None, False)
    assert eng._target_profile({"profile": "advisor"}, "default") == ("advisor", True)
    with pytest.raises(engine_mod.OrchestratorError):
        eng._target_profile({"profile": "nope"}, "default")
    eng.store.set_settings({"cross_profile_delegation": False})
    with pytest.raises(engine_mod.OrchestratorError):
        eng._target_profile({"profile": "advisor"}, "default")


# ── in-thread harness switch (Hermes model <-> Claude CLI) ───────────────────

harness = importlib.import_module("t3o.harness")


class _Agent:
    def __init__(self, provider="claude-cli", model="opus", session_id="s1"):
        self.provider, self.model, self.session_id = provider, model, session_id
        self._session_db = None


def _hermes_thread():
    return [
        {"role": "user", "content": "build the parser", "message_uid": "u1"},
        {"role": "assistant", "content": None, "message_uid": "a1",
         "tool_calls": [{"id": "c1", "function": {"name": "read_file", "arguments": '{"path": "p.py"}'}}]},
        {"role": "tool", "tool_call_id": "c1", "content": "def parse(): pass", "message_uid": "t1"},
        {"role": "assistant", "content": "Parser done.", "message_uid": "a2"},
    ]


@pytest.fixture()
def hx(eng, monkeypatch):
    monkeypatch.setattr(harness, "_store", lambda: eng.store)
    monkeypatch.setattr(harness, "_label", lambda p, m: f"{p}/{m}")
    harness._sources.clear()
    return harness


def test_fresh_chat_is_left_to_the_host(hx):
    assert hx.plan_handoff({}, _Agent(), [], cwd="/w") is None
    assert hx.plan_handoff({}, _Agent(), [{"role": "user", "content": "hi"}], cwd="/w") is None


def test_hermes_transcript_gets_full_handoff_with_clipped_tool_output(hx):
    hx._sources["s1"] = ("openai-codex", "gpt-5.5")
    plan = hx.plan_handoff({}, _Agent(), _hermes_thread(), cwd="/w")
    assert plan["reset"] and plan["strategy"] == "portable_context"
    assert "openai-codex/gpt-5.5" in plan["text"] and "claude-cli/opus" in plan["text"]
    assert "build the parser" in plan["text"] and "Parser done." in plan["text"]
    assert "read_file(p.py)" in plan["text"] and "def parse(): pass" in plan["text"]


def test_owned_native_thread_with_no_missed_turns_is_untouched(hx):
    msgs = _hermes_thread()
    state = {"session_id": "n1", "provider": "claude-cli", "owner": "s1", "cwd": "/w", "mirror_tail_uid": "a2"}
    assert hx.plan_handoff(state, _Agent(), msgs, cwd="/w") is None


def test_returning_native_thread_gets_only_missed_turns(hx):
    msgs = _hermes_thread() + [
        {"role": "user", "content": "[model switch]", "display_kind": "model_switch", "message_uid": "m1"},
        {"role": "user", "content": "now add tests", "message_uid": "u2"},
        {"role": "assistant", "content": "Tests added.", "message_uid": "a3"},
    ]
    state = {"session_id": "n1", "provider": "claude-cli", "owner": "s1", "cwd": "/w", "mirror_tail_uid": "a2"}
    plan = hx.plan_handoff(state, _Agent(), msgs, cwd="/w")
    assert plan and not plan["reset"] and plan["strategy"] == "native_resume_delta"
    assert "now add tests" in plan["text"] and "Tests added." in plan["text"]
    assert "build the parser" not in plan["text"] and "[model switch]" not in plan["text"]


def test_failed_native_turn_after_tail_is_not_a_handoff(hx):
    msgs = _hermes_thread() + [
        {"role": "user", "content": "again", "message_uid": "u2"},
        {"role": "assistant", "content": "Native runtime failed: boom", "message_uid": "a3"},
    ]
    state = {"session_id": "n1", "provider": "claude-cli", "owner": "s1", "cwd": "/w", "mirror_tail_uid": "a2"}
    assert hx.plan_handoff(state, _Agent(), msgs, cwd="/w") is None
    assert hx.plan_handoff({**state, "detached": True}, _Agent(), msgs, cwd="/w")["strategy"] == "native_resume_delta"


@pytest.mark.parametrize("change,reason", [
    ({"cwd": "/elsewhere"}, "workspace_changed"),
    ({"owner": "other"}, "copied_session"),
    ({"mirror_tail_uid": "gone"}, "rewound"),
])
def test_unresumable_native_state_restarts_with_full_handoff(hx, change, reason):
    state = {"session_id": "n1", "provider": "claude-cli", "owner": "s1", "cwd": "/w", "mirror_tail_uid": "a2", **change}
    plan = hx.plan_handoff(state, _Agent(), _hermes_thread(), cwd="/w")
    assert plan["reset"] and plan["reason"] == reason and "Parser done." in plan["text"]


def test_pending_native_fork_is_left_to_claude(hx):
    state = {"fork_from": "n0", "provider": "claude-cli", "owner": "s1", "cwd": "/w"}
    assert hx.plan_handoff(state, _Agent(), _hermes_thread(), cwd="/w") is None


def test_handoff_rides_only_the_next_turn(hx):
    seen = []

    class Runtime:
        def run_turn(self, message, *, model):
            seen.append(message)
            return iter(())

    rt = Runtime()
    hx.arm(rt, "CTX")
    rt.run_turn("hello", model="opus")
    rt.run_turn("again", model="opus")
    assert seen[0].startswith("CTX") and seen[0].endswith("hello") and seen[1] == "again"
    parts = hx.prefix([{"type": "text", "text": "hi"}], "CTX")
    assert parts[0]["text"].startswith("CTX") and parts[1]["text"] == "hi"


def test_switch_wrap_rebuilds_agent_across_harness(hx, monkeypatch):
    import types
    calls = []
    native = {"claude-cli"}
    monkeypatch.setattr(hx, "_native", lambda p: p in native)
    old = _Agent(provider="openai-codex", model="gpt-5.5", session_id="k1")
    new = _Agent(provider="claude-cli", model="opus", session_id="k1")

    srv = types.SimpleNamespace(
        _commit_agent_switch=lambda *a: calls.append("inplace"),
        _set_session_context=lambda key, cwd=None: calls.append(("ctx", key)) or [],
        _clear_session_context=lambda tokens: None,
        _session_cwd=lambda s: "/w", _session_source=lambda s: "desktop",
        _rebuild_session_agent=lambda sid, s, **kw: calls.append(("rebuild", kw["model_override"]["provider"])) or new,
        _restart_slash_worker=lambda sid, s: None,
        _persist_live_session_runtime=lambda s: calls.append("persist"),
        _persist_live_session_system_prompt=lambda s: None,
        _append_model_switch_marker=lambda s, **kw: calls.append(("marker", kw["provider"])),
        _emit_session_info=lambda sid, s: None,
    )
    monkeypatch.setattr(hx.gw, "server", lambda: srv)
    assert hx.install_switch_wrap() and hx.install_switch_wrap()
    result = types.SimpleNamespace(new_model="opus", target_provider="claude-cli", base_url="", api_key="", api_mode="")
    srv._commit_agent_switch("sid", {"session_key": "k1"}, old, result, "gpt-5.5", None)
    assert ("rebuild", "claude-cli") in calls and ("marker", "claude-cli") in calls and "inplace" not in calls
    assert hx._sources["k1"] == ("openai-codex", "gpt-5.5")
    with pytest.raises(ValueError):
        srv._commit_agent_switch("sid", {"session_key": "k1"}, old, result, "gpt-5.5", {"once": 1})
    same = types.SimpleNamespace(new_model="gpt-5.4", target_provider="openai-codex", base_url="", api_key="", api_mode="")
    srv._commit_agent_switch("sid", {"session_key": "k1"}, old, same, "gpt-5.5", None)
    assert calls[-1] == "inplace"


# ── native subagents (Muse Code / Antigravity) in the Subagents frame ───────────────────────────

def test_native_subagents_join_the_frame_with_their_own_controls(eng, monkeypatch):
    chatframe = importlib.import_module("t3o.chatframe")
    gw = importlib.import_module("t3o.gw")
    emitted, hooks = [], {}
    monkeypatch.setattr(engine_mod, "engine", lambda: eng)
    monkeypatch.setattr(eng, "live_session", lambda key, profile=None: ("sid-1", {}))
    monkeypatch.setattr(eng, "_key_of_sid", lambda sid: ("chat-1", None))
    monkeypatch.setattr(gw, "emit", lambda event, sid, payload: emitted.append((event, sid, payload)))
    monkeypatch.setattr(gw, "ok", lambda rid, result: {"id": rid, "result": result})
    monkeypatch.setattr(gw, "wrap_method", lambda name, after: hooks.__setitem__(name, after))
    monkeypatch.setattr(gw, "intercept_method", lambda name, fn: hooks.__setitem__(name, fn))
    monkeypatch.setattr(eng.store, "tasks_for_parent", lambda key: [])
    chatframe.NATIVE.clear()
    chatframe.install(eng)
    stopped, steered = [], []
    sub = {"id": "muse_s1", "goal": "reply ok", "status": "running", "model": "Muse Code"}
    chatframe.native_update("chat-1", sub, stop=lambda: stopped.append(1) or True,
                            steer=lambda text: steered.append(text) or True)
    chatframe.native_update("chat-1", sub)  # unchanged → no second event
    assert [e[0] for e in emitted] == ["subagent.start"]
    resp = {"result": {"subagents": []}}
    hooks["subagent.list"]({"session_id": "sid-1"}, resp)
    row = resp["result"]["subagents"][0]
    assert row["subagent_id"] == "native_muse_s1" and row["accepting_steer"] and row["status"] == "running"
    assert hooks["subagent.steer"]("r1", {"subagent_id": "native_muse_s1", "text": "hurry"})["result"]["status"] == "queued"
    assert hooks["subagent.interrupt"]("r2", {"subagent_id": "native_muse_s1"})["result"]["found"]
    assert stopped == [1] and steered == ["hurry"]
    chatframe.native_update("chat-1", {**sub, "status": "completed", "summary": "ok"})
    assert emitted[-1][0] == "subagent.complete" and emitted[-1][2]["summary"] == "ok"
    tail = hooks["subagent.tail"]("r3", {"subagent_id": "native_muse_s1"})["result"]["text"]
    assert "reply ok" in tail and "ok" in tail
    # Orchestrator tasks keep their own path.
    assert hooks["subagent.interrupt"]("r4", {"subagent_id": "task_missing"}) is None
