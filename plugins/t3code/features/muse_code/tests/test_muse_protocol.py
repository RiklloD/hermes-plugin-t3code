"""MSP mapping ported from T3 Code's provider-muse; no Muse process or account access."""
import json
from pathlib import Path

import pytest

from conftest import load_feature_file

protocol = load_feature_file("muse_code", "protocol.py", "t3code_muse_protocol_test")


def test_serve_args_follow_runtime_mode():
    assert protocol.serve_args(read_only=True) == ["serve", "--disable-shell", "--disable-write", "--no-session-log"]
    assert protocol.serve_args() == ["serve", "--trust-workspace"]
    assert protocol.serve_args(runtime_mode="full-access") == ["serve", "--trust-workspace", "--disable-sandbox"]
    assert protocol.approval_mode("full-access") == "allowAll"
    assert protocol.approval_mode("approval-required") == "promptUnmatched"


def test_environment_drops_the_api_key_override_only():
    env = protocol.muse_environment({"meta_api_key": "x", "PATH": "/bin"})
    assert env == {"PATH": "/bin", "MUSE_NO_AUTO_UPDATE": "1"}


def test_shell_approvals_fall_back_to_abort_for_decline():
    approval = {"availableChoices": [
        {"choiceId": "a", "decision": "approved", "scope": "once"},
        {"choiceId": "s", "decision": "approvedPolicyAmendment", "scope": "session"},
        {"choiceId": "x", "decision": "abort", "scope": "once"}]}
    choices = protocol.approval_choices(approval)
    assert choices["accept"]["choiceId"] == "a"
    assert choices["acceptForSession"]["choiceId"] == "s"
    assert choices["decline"]["choiceId"] == "x"


def test_question_answers_split_labels_and_free_text():
    request = {"questions": [
        {"id": "q1", "options": [{"label": "Yes"}, {"label": "No"}], "selection": {"mode": "single"}},
        {"id": "q2", "options": [{"label": "A"}, {"label": "B"}], "selection": {"mode": "multiple"}}]}
    rows = protocol.question_answers(request, {"q1": "Yes", "q2": ["A", "B", "and C"]})
    assert rows == [{"questionId": "q1", "selectedLabel": "Yes"},
                    {"questionId": "q2", "selectedLabels": ["A", "B"], "note": "and C"}]
    with pytest.raises(ValueError):
        protocol.question_answers(request, {"q1": [], "q2": "A"})


def test_model_rows_and_effort_resolution():
    rows = protocol.model_rows({"providerId": "meta", "models": [
        {"modelId": "muse-spark-1.2", "displayLabel": "Spark", "providerId": "meta", "isDefault": True,
         "contextLimit": 1007997, "variants": ["low", "high", "xhigh"], "defaultReasoningEffort": "high"},
        {"modelId": "muse-spark-1.2", "displayLabel": "dup", "providerId": "meta", "isDefault": False},
        {"modelId": "other", "displayLabel": "x", "providerId": "local", "isDefault": False}]})
    assert [r["id"] for r in rows] == ["muse-spark-1.2"]
    assert rows[0]["context_window"] == 1007997 and rows[0]["is_default"]
    assert protocol.resolve_effort(rows[0], "xhigh") == "xhigh"
    assert protocol.resolve_effort(rows[0], "max") == "high"  # unsupported → model default
    assert protocol.resolve_effort(None, "") == "high"
    with pytest.raises(ValueError):
        protocol.model_rows({"providerId": "local", "models": []})


def test_tool_presentation_unwraps_shell_envelopes_and_hides_housekeeping():
    envelope = json.dumps({"command": "ls", "exit_code": 2, "output": "boom\n"})
    item = {"itemId": "1", "kind": "toolCall", "tool": "bash", "status": "completed", "revision": 2,
            "args": json.dumps({"command": "ls"}), "visibleOutput": envelope}
    assert protocol.is_tool(item)
    assert protocol.tool_input(item) == {"command": "ls"}
    assert protocol.tool_output(item) == "boom\n[exit code 2]"
    assert not protocol.is_tool({"kind": "reminderChild"})
    assert not protocol.is_tool({"kind": "toolCall", "tool": "write_todos", "status": "completed"})
    assert protocol.is_tool({"kind": "toolCall", "tool": "write_todos", "status": "failed"})
    failed = {"kind": "toolCall", "tool": "edit", "status": "failed", "failureReason": "denied"}
    assert protocol.item_status(failed) == "failed" and protocol.tool_output(failed) == "denied"


def test_workspace_root_is_canonical(tmp_path):
    link = tmp_path / "link"
    link.symlink_to(tmp_path)
    assert protocol.workspace_root(str(link)) == str(Path(tmp_path).resolve())
