"""Antigravity's own slash commands for Hermes's menu.

The ACP agent announces them with ``available_commands_update`` after ``session/new`` / ``session/resume``
and runs them from the prompt text (``/plan …``), so Hermes just sends ``/name args``. Until a session
has reported its list, the last one seen (or the agent's built-ins) is offered.
"""

from __future__ import annotations

PROVIDER = "antigravity"
KEY = "acp"

DEFAULT = [
    {"name": "plan", "description": "Plan carefully before executing a task (generates an implementation plan "
                                    "artifact and awaits user approval)."},
    {"name": "logout", "description": "Sign out of Antigravity"},
]


def rows(available) -> list[dict]:
    """ACP ``AvailableCommand`` rows (``{name, description, input: {hint}}``) as menu rows."""
    return [{"name": c["name"], "description": c.get("description") or "",
             "args_hint": (c.get("input") or {}).get("hint") or ""}
            for c in available or [] if c.get("name")]


def remember(available) -> list[dict]:
    from t3code_shared import native_commands
    return native_commands.put(PROVIDER, KEY, rows(available))


def slash_commands(cwd: str = "", runtime=None) -> list[dict]:
    live = getattr(runtime, "commands", None)
    if live:
        return live
    from t3code_shared import native_commands
    return native_commands.get(PROVIDER, KEY, default=DEFAULT)
