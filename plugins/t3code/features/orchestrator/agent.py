"""T3 Code · Orchestrator (agent half): T3 Code's orchestration V2 for Hermes.

Agents delegate to child agents on any provider/model, background work is visible and stoppable,
threads fork (natively where the provider can) and merge back, follow-ups queue durably and can be
steered, and sessions recover after restarts and usage limits; scheduled tasks run on timers or
webhooks. The engine lives in ``hermes serve`` (the Desktop backend); see core/engine.py.

Called by the t3code entry point (``shared/features.register_agent_features``) when the
Orchestrator toggle is on; a failure here is contained to this feature. Every profile's plugin
manager calls ``register``; the core is loaded once per process under the fixed package name
``t3o`` so all profiles share one engine and one store.
"""

from __future__ import annotations

import importlib
import importlib.util
import logging
import sys
from pathlib import Path

logger = logging.getLogger(__name__)
HERE = Path(__file__).resolve().parent


def core():
    mod = sys.modules.get("t3o")
    if mod is None:
        spec = importlib.util.spec_from_file_location("t3o", HERE / "core" / "__init__.py",
                                                      submodule_search_locations=[str(HERE / "core")])
        mod = importlib.util.module_from_spec(spec)
        sys.modules["t3o"] = mod
        spec.loader.exec_module(mod)
    return mod


def _pre_llm_call(session_id: str = "", **kwargs):
    if not session_id:
        return None
    try:
        engine = importlib.import_module("t3o.engine").engine()
        text = engine.consume_for_prompt(session_id)
    except Exception:
        logger.debug("t3code.orchestrator: context injection failed", exc_info=True)
        return None
    return {"context": text} if text else None


def register(ctx) -> None:
    core()
    tools = importlib.import_module("t3o.tools")
    commands = importlib.import_module("t3o.commands")
    for name, schema in tools.SCHEMAS.items():
        ctx.register_tool(name=name, toolset=tools.TOOLSET, schema=schema, handler=tools.HANDLERS[name],
                          description=schema["description"].split(".")[0], emoji="🧭")
    ctx.register_hook("pre_llm_call", _pre_llm_call)
    ctx.register_command("t3", handler=commands.handle, description="T3 orchestrator: tasks, queue, schedules, recovery",
                         args_hint="[status|tasks|cancel|schedules|schedule|settings] …")
    # The engine attaches itself once the Desktop backend (hermes serve) is up; elsewhere it stays idle.
    # Native runtimes can run turns outside the Desktop backend too (messaging gateways).
    importlib.import_module("t3o.harness").install_runtime_wrap()
    engine = importlib.import_module("t3o.engine").engine()
    engine.attach_profile()
    if hasattr(ctx, "on_unload"):
        ctx.on_unload(engine.detach_profile)
