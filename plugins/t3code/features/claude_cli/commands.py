"""Claude Code's own slash commands (built-ins, skills, plugin and project commands) for Hermes's menu.

Claude Code reports them in its initialize response, per workspace. Hermes sends ``/name args`` as the
prompt and Claude Code runs it itself, as in its TUI and the Agent SDK.
"""

from __future__ import annotations

import asyncio
import tempfile

from .options import native_options, settings_for_profile

PROVIDER = "claude-cli"
HIDDEN = {"workflow-launch-exec"}  # a server-side hand-off, not something to type


def rows(commands) -> list[dict]:
    """Claude's ``commands`` (``{name, description, argumentHint, builtin}``) as menu rows."""
    out = []
    for command in commands or []:
        name, description = str(command.get("name") or ""), str(command.get("description") or "")
        # Internal hand-offs (``__remote-workflow``) and retired names stay hidden.
        if not name or name.startswith("_") or name in HIDDEN or description.startswith(("(removed)", "Renamed to")):
            continue
        out.append({"name": name, "description": description, "args_hint": command.get("argumentHint") or "",
                    "kind": "command" if command.get("builtin") else "skill"})
    return out


def cache_key(settings: dict, cwd: str) -> str:
    return f"{settings.get('config_directory') or ''}|{cwd}"


def remember(settings: dict, cwd: str, info: dict) -> list[dict]:
    """Store what a live session's Claude Code reported; returns the menu rows."""
    from t3code_shared import native_commands
    return native_commands.put(PROVIDER, cache_key(settings, cwd), rows((info or {}).get("commands")))


def _probe(settings: dict, cwd: str) -> list[dict]:
    from claude_agent_sdk import ClaudeSDKClient
    from tools.environments.local import hermes_subprocess_env
    environment = hermes_subprocess_env(inherit_credentials=True)

    async def run(workspace):
        options = native_options(settings, environment, workspace, {}, probe=True)
        async with asyncio.timeout(30):
            async with ClaudeSDKClient(options) as client:
                return rows((await client.get_server_info() or {}).get("commands"))

    if cwd:
        return asyncio.run(run(cwd))
    with tempfile.TemporaryDirectory(prefix="hermes-claude-commands-") as workspace:
        return asyncio.run(run(workspace))


def slash_commands(cwd: str = "", runtime=None, *, settings=None) -> list[dict]:
    live = getattr(runtime, "commands", None)
    if live:
        return live
    from t3code_shared import native_commands
    settings = settings_for_profile() if settings is None else settings
    # A cold workspace waits once for Claude Code's handshake; afterwards answers are instant.
    return native_commands.get(PROVIDER, cache_key(settings, cwd), lambda: _probe(settings, cwd), wait=20)
