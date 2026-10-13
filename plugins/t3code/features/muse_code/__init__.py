"""T3 Code · Muse Code: Meta's Muse Code agent as a complete Hermes agent runtime, in the model picker.

Ported from T3 Code's ``packages/provider-muse``. Discovered by Hermes's provider loader from
``<home>/plugins/model-providers/muse-code`` — a symlink to this folder that the t3code plugin
maintains. Registers only while t3code is enabled in that home and its "Muse Code" toggle is on.
"""

from __future__ import annotations

import importlib
import importlib.util
import sys
from pathlib import Path

from providers import register_provider
from providers.base import ProviderProfile

if "agent_runtime" not in ProviderProfile.__dataclass_fields__:
    raise ImportError("Muse Code needs the native-agent-runtime host patch shipped with T3 Code.")


def _shared():
    """Import t3code's shared layer as ``t3code_shared`` (the provider loader runs outside t3code)."""
    if "t3code_shared" not in sys.modules:
        root = Path(__file__).resolve().parents[2]  # plugins/t3code
        spec = importlib.util.spec_from_file_location("_t3code_shared_boot", root / "shared" / "__init__.py")
        boot = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(boot)
        boot.load(root)
    return importlib.import_module("t3code_shared.features")


class MuseCodeProfile(ProviderProfile):
    slash_namespace = "muse"

    def slash_commands(self, *, cwd="", runtime=None, **_):
        from .commands import slash_commands
        return slash_commands(cwd, runtime)

    def create_agent_runtime(self, **kwargs):
        from .catalog import settings_for_profile
        from .runtime import MuseRuntime
        kwargs["settings"] = {**kwargs.get("settings", {}), **settings_for_profile()}
        return MuseRuntime(**kwargs)

    def create_client(self, **kwargs):
        raise RuntimeError("Muse Code owns whole turns. Select a separate auxiliary provider for model-only tasks.")

    def setup_status(self, **kwargs):
        from .catalog import setup_status
        return setup_status()

    def discover_models(self, **kwargs):
        from .catalog import discover_models
        rows = discover_models(timeout=float(kwargs.get("timeout", 12)))
        for row in rows or []:
            self.model_capabilities[row["id"]] = {
                "context_window": row["context_window"], "supports_vision": True,
                "supports_tools": True, "supports_reasoning": bool(row["efforts"])}
        return rows

    def fetch_models(self, **kwargs):
        rows = self.discover_models(**kwargs)
        return [row["id"] for row in rows] if rows else ["default"]


def _feature_enabled() -> bool:
    try:
        from hermes_constants import get_hermes_home
        return _shared().feature_enabled_in_home("muse_code", Path(get_hermes_home()))
    except Exception:
        return True


if _feature_enabled():
    _shared()
    register_provider(MuseCodeProfile(
        name="muse-code", display_name="Muse Code", agent_runtime="muse-code",
        description="Meta's Muse Code agent, tools and sessions inside Hermes",
        auth_type="external_process", process_command="muse", base_url="process://muse-code",
        api_mode="chat_completions", supports_health_check=False,
        supports_vision=True, supports_vision_tool_messages=True,
        fallback_models=(), model_aliases={"default": "default"},
    ))
