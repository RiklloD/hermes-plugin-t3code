"""Native launch settings. No authentication files are read by the plugin."""

from __future__ import annotations

import json
from pathlib import Path

ACCOUNT2_DEFAULT_CONFIG_DIR = "~/.claude-account2"


def settings_for_profile() -> dict:
    """``plugins.entries.t3code.settings.claude_<key>`` (T3 Code), over the pre-merge
    ``plugins.entries.claude-cli.settings.<key>`` so older configs keep working."""
    from hermes_cli.config import load_config_readonly
    entries = ((load_config_readonly() or {}).get("plugins") or {}).get("entries") or {}
    legacy = ((entries.get("claude-cli") or {}).get("settings")) or {}
    t3code = ((entries.get("t3code") or {}).get("settings")) or {}
    current = {k[len("claude_"):]: v for k, v in t3code.items() if k.startswith("claude_") and k != "claude_cli"}
    return {**legacy, **current}


def settings_for_account2() -> dict:
    """Settings for the second Claude account.

    Inherits all settings from account 1 except ``config_directory``, which is taken
    from ``claude_second_config_directory`` and defaults to ~/.claude-account2."""
    from hermes_cli.config import load_config_readonly
    base = settings_for_profile()
    t3code = (((load_config_readonly() or {}).get("plugins") or {}).get("entries") or {}).get("t3code") or {}
    raw = t3code.get("settings") or {}
    second_dir = str(raw.get("claude_second_config_directory") or "").strip()
    if not second_dir:
        second_dir = ACCOUNT2_DEFAULT_CONFIG_DIR
    return {**base, "config_directory": second_dir}


def account_definitions() -> list[dict]:
    """One profile-scoped source for runtime, status and usage account identities."""
    return [
        {"id": "claude-cli", "label": "Account A", "settings": settings_for_profile()},
        {"id": "claude-cli-2", "label": "Account B", "settings": settings_for_account2()},
    ]


def config_directory(settings: dict) -> Path:
    import os
    return Path(settings.get("config_directory") or os.environ.get("CLAUDE_CONFIG_DIR")
                or Path.home() / ".claude").expanduser().resolve()


def subscription_env(settings: dict) -> dict:
    """Use the owning Hermes profile's child environment, never another account's token."""
    from tools.environments.local import hermes_subprocess_env
    env = hermes_subprocess_env(inherit_credentials=False)
    # On macOS, explicitly setting the default path can select a different Keychain
    # namespace from an unset CLAUDE_CONFIG_DIR. Preserve the original default login.
    if settings.get("config_directory"):
        env["CLAUDE_CONFIG_DIR"] = str(config_directory(settings))
    env.pop("CLAUDECODE", None)
    env.pop("CLAUDE_CODE_ENTRYPOINT", None)
    for key in ("CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN",
                "ANTHROPIC_BASE_URL", "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX",
                "CLAUDE_CODE_USE_FOUNDRY"):
        env.pop(key, None)
    return env


def login_command(settings: dict) -> str:
    import shlex
    prefix = f"CLAUDE_CONFIG_DIR={shlex.quote(str(config_directory(settings)))} " if settings.get("config_directory") else ""
    return f"{prefix}{shlex.quote(str(settings.get('executable') or 'claude'))} auth login --claudeai"


def account_status(settings: dict) -> dict:
    """Only safe CLI-reported identity metadata leaves the native authentication boundary."""
    import subprocess
    command = login_command(settings)
    try:
        binary = executable(settings)
    except FileNotFoundError as exc:
        return {"available": False, "logged_in": False, "detail": str(exc), "login_command": command}
    try:
        done = subprocess.run([binary, "auth", "status"], capture_output=True, text=True,
                              env=subscription_env(settings), timeout=10, check=False)
        info = json.loads(done.stdout)
        if not isinstance(info, dict):
            info = {}
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return {"available": True, "logged_in": False, "detail": "Claude login status unavailable.",
                "login_command": command}
    logged_in = bool(info.get("loggedIn"))
    return {"available": True, "logged_in": logged_in,
            "plan": str(info.get("subscriptionType") or ""),
            "email": str(info.get("emailAddress") or info.get("email") or ""),
            "detail": "Native Claude login" if logged_in else "Sign in to use this account.",
            "login_command": command}


def executable(settings: dict) -> str:
    from hermes_platform.resolver import locate_command
    candidate = str(settings.get("executable") or "claude")
    resolved = locate_command(candidate, known_dirs=(str(Path.home() / ".local/bin"),
                               str(Path.home() / ".claude/local"), "/opt/homebrew/bin", "/usr/local/bin"))
    if not resolved.found:
        raise FileNotFoundError("Claude Code is missing. Install the official claude CLI and run claude auth login.")
    return resolved.command[0]


def native_options(settings: dict, environment: dict, cwd: str, state: dict, *, probe=False, model="default"):
    from .model_selection import context_policy, launch_model
    from claude_agent_sdk import ClaudeAgentOptions
    env = dict(environment)
    # The SDK merges env on top of the parent. Empty the stripped keys too, so the SDK cannot re-inherit them.
    import os
    env.update({key: "" for key in os.environ if key not in environment})
    env.pop("CLAUDE_CODE_ENTRYPOINT", None)
    env["CLAUDECODE"] = ""
    config_dir = str(settings.get("config_directory") or "")
    if config_dir:
        env["CLAUDE_CONFIG_DIR"] = str(Path(config_dir).expanduser().resolve())
    # Native login stays native. A profile's API credentials must not override subscription routing by accident.
    # CLAUDE_CODE_OAUTH_TOKEN is also cleared: accounts must not share an inherited OAuth token.
    if settings.get("auth_mode", "subscription") == "subscription":
        for key in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL",
                    "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX", "CLAUDE_CODE_USE_FOUNDRY",
                    "CLAUDE_CODE_OAUTH_TOKEN"):
            env[key] = ""
    permission_mode = str(settings.get("permission_mode") or "default")
    if settings.get("auth_mode", "subscription") not in {"subscription", "native"}:
        raise ValueError("auth_mode must be subscription or native")
    if permission_mode not in {"default", "acceptEdits", "plan", "dontAsk", "auto", "bypassPermissions"}:
        raise ValueError(f"Unknown Claude permission mode: {permission_mode}")
    options = ClaudeAgentOptions(
        cli_path=executable(settings), cwd=cwd, env=env,
        tools={"type": "preset", "preset": "claude_code"},
        system_prompt={"type": "preset", "preset": "claude_code"},
        setting_sources=list(settings.get("setting_sources", ["user", "project", "local"])),
        permission_mode=permission_mode, include_partial_messages=True,
        forward_subagent_text=True,
    )
    if not probe:
        # Hermes does not build this agent's system prompt, so the profile's SOUL.md is appended to Claude Code's.
        # It is rebuilt on every launch, so resumed sessions and relaunches follow the current file.
        try:
            from t3code_shared import identity
            soul = identity.resolve(environment)
        except Exception:
            soul = None
        if soul:
            options.system_prompt = {"type": "preset", "preset": "claude_code", "append": soul["text"]}
        policy = context_policy(model, settings)
        options.model = launch_model(model, policy)
        if policy != "auto":
            # Session-owned launch overrides; never change global Claude settings.
            options.env["CLAUDE_CODE_DISABLE_1M_CONTEXT"] = "1" if policy == "200k" else ""
            options.env["CLAUDE_CODE_AUTO_COMPACT_WINDOW"] = "200000" if policy == "200k" else "1000000"
    if state.get("session_id"):
        if state.get("config_directory", "") != config_dir:
            raise ValueError("This native session uses a different Claude config directory. Start a new chat.")
        options.resume = state["session_id"]
    elif state.get("fork_from"):
        # A T3 orchestrator fork: Claude copies the source session under a new id on first use.
        if state.get("config_directory", "") != config_dir:
            raise ValueError("The forked native session uses a different Claude config directory. Start a new chat.")
        options.resume = state["fork_from"]
        options.fork_session = True
        if state.get("fork_at"):
            # Pin the fork to the moment it was taken, not the source's latest state.
            options.resume_session_at = state["fork_at"]
    if probe:
        options.settings = json.dumps({"disableAllHooks": True})
        options.strict_mcp_config = True
        options.mcp_servers = {}
        options.env["ENABLE_CLAUDEAI_MCP_SERVERS"] = "false"
        options.extra_args["no-session-persistence"] = None
    return options
