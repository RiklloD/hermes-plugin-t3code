"""T3 orchestrator tools for native Claude sessions.

Claude CLI runs its own agent loop, so Hermes tools never reach it. When T3 Code's Orchestrator
feature is loaded in this process, its tools are exposed to Claude as an in-process SDK MCP server
(``mcp__t3__t3_delegate`` …), letting a Claude parent delegate to Codex, GLM and others. The
calling Hermes session is resolved from the turn's session context (the runtime worker runs in a
copy of the turn's contextvars).
"""

from __future__ import annotations

import asyncio
import sys


def server():
    tools_mod = sys.modules.get("t3o.tools")
    if tools_mod is None:
        return None
    from claude_agent_sdk import create_sdk_mcp_server, tool

    def make(name: str, schema: dict):
        @tool(name, schema["description"], schema["parameters"])
        async def handler(args):
            from gateway.session_context import get_session_env
            session_id = get_session_env("HERMES_SESSION_ID")
            text = await asyncio.to_thread(tools_mod.HANDLERS[name], dict(args or {}), session_id=session_id)
            return {"content": [{"type": "text", "text": text}]}
        return handler

    return create_sdk_mcp_server("t3", tools=[make(n, s) for n, s in tools_mod.SCHEMAS.items()])
