"""Computer Use Plus tools for native Claude sessions.

Claude CLI runs its own agent loop, so Hermes plugin tools never reach it. When the computer-use-plus
plugin is loaded in this process, its tools are exposed to Claude as an in-process SDK MCP server
(``mcp__computer-use__click`` …). Every call goes through the plugin's own handler, so the Hermes
Desktop screen share, cursor, Stop and Take over work exactly as in a Hermes-native session. The
calling Hermes session is resolved from the turn's session context, like t3_bridge.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

SERVER = "computer-use"
# Observation only: never changes the desktop, so it needs no permission prompt.
READ_ONLY = ("list_apps", "get_app_state", "query")


def _codex():
    """The plugin's codex module, whatever package name the Hermes loader gave it."""
    for module in list(sys.modules.values()):
        path = getattr(module, "__file__", None) or ""
        if path and Path(path).name == "codex.py" and Path(path).parent.name == "computer-use-plus" \
                and hasattr(module, "handle") and hasattr(module, "TOOL_NAMES"):
            return module
    return None


def _session_id() -> str:
    from gateway.session_context import get_session_env
    return get_session_env("HERMES_SESSION_ID")


def _content(out) -> dict:
    """Plugin handler result (text, or Hermes multimodal dict) -> MCP tool result."""
    if isinstance(out, dict) and out.get("_multimodal"):
        items = []
        for part in out.get("content") or []:
            if part.get("type") == "text" and part.get("text"):
                items.append({"type": "text", "text": part["text"]})
            elif part.get("type") == "image_url":
                url = (part.get("image_url") or {}).get("url", "")
                if url.startswith("data:") and ";base64," in url:
                    mime, data = url[5:].split(";base64,", 1)
                    items.append({"type": "image", "data": data, "mimeType": mime})
        return {"content": items or [{"type": "text", "text": out.get("text_summary") or "OK"}]}
    text = str(out)
    result = {"content": [{"type": "text", "text": text}]}
    if text.startswith('{"ok": false'):
        result["is_error"] = True
    return result


def server():
    codex = _codex()
    if codex is None:
        return None
    from claude_agent_sdk import create_sdk_mcp_server, tool

    def call(name: str, args: dict):
        # Claude takes images in tool results, whatever the profile's main model is.
        override = getattr(codex, "IMAGES_OVERRIDE", None)
        if override is not None:
            override.set(True)
        return codex.handle(name, args, session_id=_session_id())

    def make(name: str):
        schema = codex.schema(name)
        description = schema["description"]
        if name == "get_app_state":
            # Hermes puts the server's instructions in its system prompt; Claude's loop never sees that prompt.
            description += "\n\n" + codex.instructions()

        @tool(name, description, schema["parameters"])
        async def handler(args):
            return _content(await asyncio.to_thread(call, name, dict(args or {})))
        return handler

    return create_sdk_mcp_server(SERVER, tools=[make(n) for n in codex.TOOL_NAMES])


def preapproved() -> list[str]:
    return [f"mcp__{SERVER}__{n}" for n in READ_ONLY]


def turn_ended() -> None:
    """Codex's turn-ended notification: the native cursor is put away (Hermes-native turns get this from
    the plugin's post_llm_call hook)."""
    codex = _codex()
    if codex is not None:
        try:
            codex.turn_ended(_session_id())
        except Exception:
            pass
