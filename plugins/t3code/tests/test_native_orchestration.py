"""The loopback MCP endpoint that gives Muse Code / Antigravity the Orchestrator's t3_* tools."""
import json
import sys
import types
import urllib.error
import urllib.request

import pytest

import t3code_shared.orchestration as orch


@pytest.fixture()
def tools(monkeypatch):
    mod = types.ModuleType("t3o.tools")
    calls = []
    mod.SCHEMAS = {"t3_task": {"description": "Read a task.", "parameters": {"type": "object", "properties": {}}}}
    mod.HANDLERS = {"t3_task": lambda args, session_id=None: calls.append((args, session_id)) or "done"}
    monkeypatch.setitem(sys.modules, "t3o.tools", mod)
    return calls


def _post(url, token, body):
    req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST",
                                 headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(req, timeout=5) as resp:
        return resp.status, (json.loads(resp.read() or b"null"))


def test_endpoint_needs_the_orchestrator(monkeypatch):
    monkeypatch.delitem(sys.modules, "t3o.tools", raising=False)
    assert orch.mcp_endpoint(lambda: "chat") is None


def test_tools_run_for_the_runtimes_chat_and_tokens_are_scoped(tools):
    url, token = orch.mcp_endpoint(lambda: "chat-42")
    status, init = _post(url, token, {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                                      "params": {"protocolVersion": "2025-03-26"}})
    assert status == 200 and init["result"]["protocolVersion"] == "2025-03-26"
    assert _post(url, token, {"jsonrpc": "2.0", "method": "notifications/initialized"})[0] == 202
    listed = _post(url, token, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})[1]
    assert [t["name"] for t in listed["result"]["tools"]] == ["t3_task"]
    called = _post(url, token, {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                                "params": {"name": "t3_task", "arguments": {"task_id": "x"}}})[1]
    assert called["result"]["content"][0]["text"] == "done" and tools == [({"task_id": "x"}, "chat-42")]
    orch.revoke(token)
    with pytest.raises(urllib.error.HTTPError) as err:
        _post(url, token, {"jsonrpc": "2.0", "id": 4, "method": "tools/list"})
    assert err.value.code == 401


def test_report_subagent_is_a_noop_without_the_orchestrator(monkeypatch):
    monkeypatch.delitem(sys.modules, "t3o.chatframe", raising=False)
    orch.report_subagent("chat", {"id": "x", "goal": "g", "status": "running"})  # must not raise
