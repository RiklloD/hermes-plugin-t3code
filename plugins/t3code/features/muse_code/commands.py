"""Muse Code's own slash commands for Hermes's menu: its skills, and the TUI commands MSP serves as methods.

MSP has no command list: Muse's TUI owns its slash menu. Skills are the session's user-invocable set
(``skill/list``: the live session's, or a throwaway read-only host's before one exists) and run as a
``skill`` input part; the built-ins below map to their MSP methods (see ``MuseRuntime._builtin``).
"""

from __future__ import annotations

import tempfile
from typing import Optional

PROVIDER = "muse-code"

BUILTINS = [
    {"name": "compact", "description": "Compact Muse's conversation context now", "kind": "command"},
    {"name": "goal", "description": "Set Muse's goal and let it work until the goal is met, or clear, pause or resume it",
     "args_hint": "<objective> | clear | pause | resume", "kind": "command"},
    {"name": "rename", "description": "Rename this Muse session", "args_hint": "<name>", "kind": "command"},
]
BUILTIN_NAMES = {row["name"] for row in BUILTINS}


def parse(text: str) -> Optional[tuple[str, str]]:
    """``("name", "args")`` for a ``/name args`` prompt, else None."""
    if not text.startswith("/") or text.startswith("//"):
        return None
    name, _, args = text[1:].partition(" ")
    name = name.strip()
    return (name, args.strip()) if name and "/" not in name else None


def skill_rows(entries) -> list[dict]:
    """``skill/list`` entries (``SkillCatalogEntry``) as menu rows; the selector is what a skill part submits."""
    return [{"name": e["selector"], "description": e.get("description") or e.get("displayName") or "",
             "args_hint": e.get("argumentHint") or "", "kind": "skill"}
            for e in entries or [] if e.get("selector") and e["selector"] not in BUILTIN_NAMES]


def _probe_skills(settings: dict, cwd: str) -> list[dict]:
    """``skill/list`` from a read-only host (no shell, no writes, ephemeral session: nothing persists)."""
    from t3code_shared.stdio_rpc import StdioRpc, uuid7
    from tools.environments.local import hermes_subprocess_env
    from . import protocol
    from .catalog import executable
    with tempfile.TemporaryDirectory(prefix="hermes-muse-commands-") as scratch:
        workspace = cwd or scratch
        rpc = StdioRpc([executable(settings), *protocol.serve_args(read_only=True)], cwd=workspace,
                       env=protocol.muse_environment(hermes_subprocess_env(inherit_credentials=True)),
                       name="Muse Code", on_notification=lambda *_: None,
                       on_request=lambda request_id, *_: rpc.respond(request_id, {}))
        try:
            rpc.request("initialize", protocol.initialize_params(read_only=True), timeout=8)
            rpc.notify("initialized")
            session_id = uuid7()
            rpc.request("session/start", {"sessionId": session_id, "commandId": uuid7(), "providerId": "meta",
                                          "workspaceRoot": protocol.workspace_root(workspace),
                                          "approvalMode": protocol.approval_mode("approval-required")}, 30)
            return skill_rows((rpc.request("skill/list", {"sessionId": session_id}, 15) or {}).get("skills"))
        finally:
            rpc.close(5)


def slash_commands(cwd: str = "", runtime=None) -> list[dict]:
    skills = getattr(runtime, "skills", None)
    if skills is None:
        from t3code_shared import native_commands
        from .catalog import settings_for_profile
        settings = settings_for_profile()
        skills = native_commands.get(PROVIDER, cwd, lambda: _probe_skills(settings, cwd), wait=8)
    return BUILTINS + list(skills)
