"""T3 Code · Claude CLI: Claude Code as a complete Hermes agent runtime, in the normal model picker.

Model providers are discovered by Hermes's provider loader, not the plugin manager, from
``<home>/plugins/model-providers/claude-cli`` — a symlink to this folder that the t3code plugin
maintains (``shared/features.ensure_provider_link``). The provider registers only while t3code is
enabled in that home and its "Claude CLI" toggle is on.

Two providers are registered: ``claude-cli`` (account A, existing config directory) and
``claude-cli-2`` (account B, ``claude_second_config_directory`` setting, defaults to
~/.claude-account2). Both share the same binary and SDK; account binding is through the child
process CLAUDE_CONFIG_DIR.
"""

from __future__ import annotations

from pathlib import Path

from providers import register_provider
from providers.base import ProviderProfile
from .model_selection import ModelCapabilities

if "agent_runtime" not in ProviderProfile.__dataclass_fields__:
    raise ImportError("Claude CLI needs the native-agent-runtime host patch shipped with this plugin.")


class ClaudeCLIProfile(ProviderProfile):
    slash_namespace = "claude"

    def account_settings(self):
        from .options import settings_for_profile
        return settings_for_profile()

    def slash_commands(self, *, cwd="", runtime=None, **_):
        from .commands import slash_commands
        return slash_commands(cwd, runtime, settings=self.account_settings())

    def create_agent_runtime(self, **kwargs):
        from .sdk_runtime import ClaudeRuntime
        kwargs["settings"] = {**kwargs.get("settings", {}), **self.account_settings()}
        return ClaudeRuntime(**kwargs)

    def create_client(self, **kwargs):
        raise RuntimeError("Claude CLI owns whole turns. Select a separate auxiliary provider for model-only tasks.")

    def setup_status(self, **kwargs):
        from .options import account_status
        return account_status(self.account_settings())

    def discover_models(self, **kwargs):
        from .catalog import discover_models
        rows = discover_models(timeout=float(kwargs.get("timeout", 25)), settings=self.account_settings())
        for row in rows or []:
            self.model_capabilities[row["id"]] = {
                "context_window": row["context_window"], "supports_vision": True,
                "supports_tools": True, "supports_reasoning": row["supports_reasoning"],
            }
            if "[200k]" not in row["id"]:
                for alias in row.get("aliases", []):
                    self.model_capabilities[alias] = self.model_capabilities[row["id"]]
                    self.model_capabilities[f"{alias}[200k]"] = {
                        **self.model_capabilities[row["id"]], "context_window": 200_000,
                    }
        return rows

    def fetch_models(self, **kwargs):
        rows = self.discover_models(**kwargs)
        return [row["id"] for row in rows] if rows else ["default", "sonnet", "opus", "haiku"]


class ClaudeCLI2Profile(ClaudeCLIProfile):
    """A separate provider identity binds native conversations to account B."""

    def account_settings(self):
        from .options import settings_for_account2
        return settings_for_account2()


def _feature_enabled() -> bool:
    """t3code enabled + Claude CLI toggle on in the home being scanned. Fails open."""
    try:
        import importlib
        import importlib.util
        import sys

        from hermes_constants import get_hermes_home

        root = Path(__file__).resolve().parents[2]  # plugins/t3code
        if "t3code_shared" not in sys.modules:
            spec = importlib.util.spec_from_file_location("_t3code_shared_boot", root / "shared" / "__init__.py")
            boot = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(boot)
            boot.load(root)
        features = importlib.import_module("t3code_shared.features")
        return features.feature_enabled_in_home("claude_cli", Path(get_hermes_home()))
    except Exception:
        return True


if _feature_enabled():
    _profile = ClaudeCLIProfile(
        name="claude-cli", display_name="Claude CLI", agent_runtime="claude-cli",
        description="Claude Code's native agent, tools and sessions inside Hermes",
        auth_type="external_process", process_command="claude", base_url="process://claude-cli",
        api_mode="chat_completions", supports_health_check=False,
        supports_vision=True, supports_vision_tool_messages=True,
        fallback_models=(),
        model_aliases={"default": "default", "sonnet": "sonnet", "opus": "opus", "haiku": "haiku"},
        model_capabilities=ModelCapabilities(),
    )
    _profile.model_capabilities.discover = _profile.discover_models
    register_provider(_profile)

    _profile2 = ClaudeCLI2Profile(
        name="claude-cli-2", display_name="Claude CLI 2", agent_runtime="claude-cli",
        description="Second Claude account — uses claude_second_config_directory",
        auth_type="external_process", process_command="claude", base_url="process://claude-cli-2",
        api_mode="chat_completions", supports_health_check=False,
        supports_vision=True, supports_vision_tool_messages=True,
        fallback_models=(),
        model_aliases={"default": "default", "sonnet": "sonnet", "opus": "opus", "haiku": "haiku"},
        model_capabilities=ModelCapabilities(),
    )
    _profile2.model_capabilities.discover = _profile2.discover_models
    register_provider(_profile2)
