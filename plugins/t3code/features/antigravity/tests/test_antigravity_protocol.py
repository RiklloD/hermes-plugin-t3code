"""Antigravity ACP mapping ported from T3 Code; no agent process or Google account access."""
import json

import pytest

from conftest import load_feature_file

protocol = load_feature_file("antigravity", "protocol.py", "t3code_antigravity_protocol_test")
release = load_feature_file("antigravity", "release.py", "t3code_antigravity_release_test")

PERMISSION = {"toolCall": {"toolCallId": "abc", "title": "ls", "kind": "execute"}, "options": [
    {"optionId": "allow_always", "kind": "allow_always"}, {"optionId": "allow_once", "kind": "allow_once"},
    {"optionId": "reject_once", "kind": "reject_once"}]}
QUESTION = {"toolCall": {"toolCallId": "interaction_1", "title": "Pick one"}, "options": [
    {"optionId": "o1", "name": "Red"}, {"optionId": "o2", "name": "Blue"}]}


def test_environment_keeps_only_the_configured_credential(tmp_path):
    base = {"GEMINI_API_KEY": "ambient", "GOOGLE_CLOUD_PROJECT": "p", "BROWSER": "firefox", "PATH": "/bin"}
    auth = protocol.auth_config({"auth_method": "agent-platform", "api_key": "k"})
    env = protocol.environment(base, tmp_path, {"harness": "/rt/localharness_external"}, auth)
    assert env["GOOGLE_API_KEY"] == "k" and "GEMINI_API_KEY" not in env and "GOOGLE_CLOUD_PROJECT" not in env
    assert env["GEMINI_HOME"] == str(tmp_path) and env["AGY_ACP_FORCE_FILE_STORAGE"] == "1"
    assert env["ANTIGRAVITY_HARNESS_PATH"] == "/rt/localharness_external"
    assert env["BROWSER"] != "firefox"  # the agent never opens a browser itself
    with pytest.raises(ValueError):
        protocol.auth_config({"auth_method": "password"})


def test_profile_settings_hold_no_credential(tmp_path):
    auth = protocol.auth_config({"auth_method": "oauth-business", "gcp_project": "proj", "gcp_location": "eu"})
    protocol.prepare_profile(tmp_path, auth)
    assert json.loads((tmp_path / "antigravity-acp" / "settings.json").read_text()) == {
        "auth": {"type": "oauth-business"}, "gcp": {"project": "proj", "location": "eu"}}


def test_permission_and_question_mapping():
    assert protocol.permission_mode("full-access") == "yolo"
    assert protocol.permission_mode("auto-accept-edits") == "auto_edit"
    assert protocol.permission_mode("approval-required") == "default"
    assert not protocol.is_question(PERMISSION) and protocol.is_question(QUESTION)
    assert protocol.permission_option(PERMISSION, True) == "allow_once"
    assert protocol.permission_option(PERMISSION, False) == "reject_once"
    assert protocol.question_answer(QUESTION, "Blue") == "o2"
    assert protocol.question_answer(QUESTION, ["o1"]) == "o1"
    assert protocol.question_answer(QUESTION, "Green") is None


def test_tool_calls_normalize_native_command_fields():
    call = {"toolCallId": "t", "kind": "execute", "title": "ls", "status": "completed",
            "rawInput": {"CommandLine": "ls -a", "Cwd": "/w"},
            "rawOutput": {"combinedOutput": "a\r\nb\r\n", "exitCode": 1}}
    assert protocol.tool_name(call) == "shell"
    assert protocol.tool_input(call) == {"command": "ls -a", "Cwd": "/w", "title": "ls"}
    assert protocol.tool_output(call) == "a\r\nb\r\n\n[exit code 1]"
    assert protocol.tool_output({"rawOutput": "Tool execution failed"}) == "Tool execution failed"
    assert protocol.tool_name({"title": "Running start_subagent", "kind": "other"}) == "subagent"


def test_model_options_flatten_groups():
    options = [{"id": "model", "type": "select", "currentValue": "b", "options": [
        {"value": "a", "name": "A"}, {"group": "g", "options": [{"value": "b", "name": "B"}]}]}]
    assert [o["value"] for o in protocol.model_options(options)] == ["a", "b"]
    assert protocol.current_model(options) == "b"


def test_runtime_resolution_prefers_hermes_then_t3(tmp_path, monkeypatch, hermes_home):
    monkeypatch.setattr(release.Path, "home", classmethod(lambda cls: tmp_path))
    assert release.resolve() is None
    for base in (tmp_path / ".t3" / "tools" / "antigravity-acp", release.managed_dir()):
        version = base / release.platform_key() / "versions" / "sha"
        version.mkdir(parents=True)
        for name in release.NAMES.values():
            (version / name).write_text("")
            (version / name).chmod(0o755)
        (base / release.platform_key() / "active.json").write_text('{"releaseId": "sha"}')
        assert release.resolve()["executable"].startswith(str(base))
