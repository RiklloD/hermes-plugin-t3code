"""T3 Code for Hermes — agent half. One plugin, independent features (see README.md).

Only features with agent-side code start here (today: the Orchestrator). Each starts behind its
own boundary (``shared/features.guarded``): a feature that fails to import or register is logged
and reported on the T3 Code settings page, and the rest of the plugin keeps loading.
"""

from __future__ import annotations

import importlib
import importlib.util
import logging
from pathlib import Path

logger = logging.getLogger("t3code")
ROOT = Path(__file__).resolve().parent


def _shared():
    spec = importlib.util.spec_from_file_location("_t3code_shared_boot", ROOT / "shared" / "__init__.py")
    boot = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(boot)
    boot.load(ROOT)
    return importlib.import_module("t3code_shared.features")


def register(ctx) -> None:
    _shared().register_agent_features(ctx)
