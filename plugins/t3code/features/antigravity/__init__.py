"""T3 Code · Antigravity: Google's official Antigravity ACP agent as a complete Hermes agent runtime.

Ported from T3 Code's Antigravity driver (``apps/server/src/provider/*Antigravity*``,
``AntigravityAdapterV2``). Discovered by Hermes's provider loader from
``<home>/plugins/model-providers/antigravity`` — a symlink to this folder that the t3code plugin
maintains. Registers only while t3code is enabled in that home and its "Antigravity" toggle is on.
"""

from __future__ import annotations

import importlib
import importlib.util
import sys
from pathlib import Path

from providers import register_provider
from providers.base import ProviderProfile

if "agent_runtime" not in ProviderProfile.__dataclass_fields__:
    raise ImportError("Antigravity needs the native-agent-runtime host patch shipped with T3 Code.")


def _shared():
    """Import t3code's shared layer as ``t3code_shared`` (the provider loader runs outside t3code)."""
    if "t3code_shared" not in sys.modules:
        root = Path(__file__).resolve().parents[2]  # plugins/t3code
        spec = importlib.util.spec_from_file_location("_t3code_shared_boot", root / "shared" / "__init__.py")
        boot = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(boot)
        boot.load(root)
    return importlib.import_module("t3code_shared.features")


class AntigravityProfile(ProviderProfile):
    slash_namespace = "agy"

    def slash_commands(self, *, cwd="", runtime=None, **_):
        from .commands import slash_commands
        return slash_commands(cwd, runtime)

    def create_agent_runtime(self, **kwargs):
        from .catalog import settings_for_profile
        from .runtime import AntigravityRuntime
        kwargs["settings"] = {**kwargs.get("settings", {}), **settings_for_profile()}
        return AntigravityRuntime(**kwargs)

    def create_client(self, **kwargs):
        raise RuntimeError("Antigravity owns whole turns. Select a separate auxiliary provider for model-only tasks.")

    def setup_status(self, **kwargs):
        from .catalog import setup_status
        return setup_status()

    def discover_models(self, **kwargs):
        from .catalog import discover_models
        rows = discover_models(timeout=float(kwargs.get("timeout", 90)))
        for row in rows or []:
            self.model_capabilities[row["id"]] = {
                "context_window": row["context_window"], "supports_vision": True,
                "supports_tools": True, "supports_reasoning": True}
        return rows

    def fetch_models(self, **kwargs):
        rows = self.discover_models(**kwargs)
        return [row["id"] for row in rows] if rows else ["default"]


def _feature_enabled() -> bool:
    try:
        from hermes_constants import get_hermes_home
        return _shared().feature_enabled_in_home("antigravity", Path(get_hermes_home()))
    except Exception:
        return True


def _process_command() -> str:
    """The installed runtime's path, so Hermes's structural "is it installed" check is truthful."""
    try:
        from .catalog import runtime, settings_for_profile
        return runtime(settings_for_profile())["executable"]
    except Exception:
        return "agy_acp_server.par"


if _feature_enabled():
    _shared()
    register_provider(AntigravityProfile(
        name="antigravity", display_name="Antigravity", agent_runtime="antigravity",
        description="Google's Antigravity agent (Gemini, Claude and GPT models) inside Hermes",
        auth_type="external_process", process_command=_process_command(), base_url="process://antigravity",
        api_mode="chat_completions", supports_health_check=False,
        supports_vision=True, supports_vision_tool_messages=True,
        fallback_models=(), model_aliases={"default": "default"},
    ))
