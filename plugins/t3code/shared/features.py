"""The T3 Code feature table, the per-feature toggles and the isolation every entry point shares.

One plugin, independent features. Each feature lives in ``features/<key>/`` and is started on its
own: a feature that fails to import, register or mount is recorded here as ``failed`` and the
others carry on. Entry points:

* agent half   — ``__init__.register`` → :func:`register_agent_features`
* REST backend — ``dashboard/plugin_api.py`` → :func:`load_feature_module` per feature
* Native providers (Claude CLI, Muse Code, Antigravity) — ``features/<key>/__init__.py`` (provider
  discovery) → :func:`feature_enabled_in_home`, :func:`provider_settings`

Toggles are ``plugins.entries.t3code.settings.<key>`` (declared in ``plugin.yaml``'s
``config_schema``, so the Hermes plugin manager renders them as switches). Missing = on.
"""

from __future__ import annotations

import importlib.util
import logging
import os
import sys
import threading
import time
import traceback
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Any, Callable, Dict, Mapping, Optional

logger = logging.getLogger("t3code")

PLUGIN_ID = "t3code"
ROOT = Path(__file__).resolve().parent.parent
FEATURES_DIR = ROOT / "features"


@dataclass(frozen=True)
class Feature:
    key: str
    label: str
    #: Plugin name this feature used to ship as (storage / config migration).
    legacy: str
    #: ``features/<key>/agent.py`` exposes ``register(ctx)``.
    agent: bool = False
    #: ``features/<key>/api.py`` exposes a FastAPI ``router``.
    api: bool = False
    #: Takes effect on the next Hermes backend restart (agent-side code), not live.
    restart: bool = False


FEATURES: Dict[str, Feature] = {
    f.key: f
    for f in (
        Feature("inbox", "Inbox", "t3-inbox", api=True),
        Feature("usage", "Usage", "t3-usage", api=True),
        Feature("goals", "Goals", "", api=True),
        Feature("orchestrator", "Orchestrator", "t3-orchestrator", agent=True, api=True, restart=True),
        Feature("claude_cli", "Claude CLI", "claude-cli", api=True, restart=True),
        Feature("muse_code", "Muse Code", "muse-code", restart=True),
        Feature("antigravity", "Antigravity", "antigravity", restart=True),
    )
}


# ── toggles ────────────────────────────────────────────────────────────────────────────────────

def settings_from_config(cfg: Optional[Mapping[str, Any]]) -> Mapping[str, Any]:
    """``plugins.entries.t3code.settings`` of one loaded config (``{}`` when absent)."""
    entries = ((cfg or {}).get("plugins") or {}).get("entries") or {}
    entry = entries.get(PLUGIN_ID) if isinstance(entries, Mapping) else None
    settings = entry.get("settings") if isinstance(entry, Mapping) else None
    return settings if isinstance(settings, Mapping) else {}


def toggles_from_config(cfg: Optional[Mapping[str, Any]]) -> Dict[str, bool]:
    """``{feature: enabled}`` for every feature; anything not explicitly ``false`` is on."""
    raw = settings_from_config(cfg)
    return {key: raw.get(key) is not False for key in FEATURES}


def current_toggles() -> Dict[str, bool]:
    """Toggles of the active profile (request-scoped inside the dashboard backend). Fails open."""
    try:
        from hermes_cli.config import load_config_readonly
        return toggles_from_config(load_config_readonly() or {})
    except Exception:
        logger.debug("t3code: cannot read feature toggles; treating all as on", exc_info=True)
        return {key: True for key in FEATURES}


def feature_enabled(key: str) -> bool:
    return current_toggles().get(key, True)


def provider_settings(prefix: str, toggle: str) -> Dict[str, Any]:
    """A native provider's options: ``<prefix><key>`` settings of the active profile, prefix removed."""
    from hermes_cli.config import load_config_readonly
    raw = settings_from_config(load_config_readonly() or {})
    return {k[len(prefix):]: v for k, v in raw.items() if k.startswith(prefix) and k != toggle}


def read_home_config(home: Path) -> Mapping[str, Any]:
    """``<home>/config.yaml`` parsed without importing ``hermes_cli`` (safe during provider discovery)."""
    path = Path(home) / "config.yaml"
    try:
        text = path.read_text(encoding="utf-8-sig")
    except OSError:
        return {}
    try:
        from utils import fast_safe_load
        data = fast_safe_load(text)
    except Exception:
        import yaml
        data = yaml.safe_load(text)
    return data if isinstance(data, Mapping) else {}


def feature_enabled_in_home(key: str, home: Path) -> bool:
    """Whether ``key`` runs in ``home``: the t3code plugin is enabled there and the toggle is on.

    Used where no plugin context exists (model-provider discovery). Unreadable config fails open,
    so a parse hiccup never silently removes a provider the user had.
    """
    try:
        cfg = read_home_config(home)
        plugins = cfg.get("plugins") or {}
        enabled = plugins.get("enabled")
        disabled = plugins.get("disabled") or []
        if PLUGIN_ID in disabled:
            return False
        if isinstance(enabled, list) and PLUGIN_ID not in enabled:
            return False
        return toggles_from_config(cfg).get(key, True)
    except Exception:
        logger.debug("t3code: cannot read %s toggle in %s; leaving it on", key, home, exc_info=True)
        return True


# ── status (one process-wide table; the REST backend reports it) ───────────────────────────────

_status_lock = threading.Lock()
_status: Dict[str, Dict[str, Dict[str, Any]]] = {}


def set_status(key: str, part: str, state: str, error: Optional[str] = None) -> None:
    """Record ``state`` (``running`` / ``disabled`` / ``failed``) for one part (``agent`` / ``api``)."""
    with _status_lock:
        _status.setdefault(key, {})[part] = {"state": state, "error": error, "at": time.time()}


def status_snapshot() -> Dict[str, Dict[str, Dict[str, Any]]]:
    with _status_lock:
        return {k: {p: dict(v) for p, v in parts.items()} for k, parts in _status.items()}


def _error_text(exc: BaseException) -> str:
    return "".join(traceback.format_exception_only(type(exc), exc)).strip()


def guarded(key: str, part: str, fn: Callable[[], Any]) -> bool:
    """Run one feature's startup step; a failure is logged and recorded, never raised."""
    try:
        fn()
    except Exception as exc:  # isolation boundary: one feature must not take the others down
        logger.exception("t3code: %s %s failed to start", key, part)
        set_status(key, part, "failed", _error_text(exc))
        return False
    set_status(key, part, "running")
    return True


# ── loading feature code ───────────────────────────────────────────────────────────────────────

def load_feature_module(key: str, filename: str) -> ModuleType:
    """Import ``features/<key>/<filename>`` under a stable private module name (once per process)."""
    name = f"t3code_{key}_{Path(filename).stem}"
    mod = sys.modules.get(name)
    if mod is not None:
        return mod
    path = FEATURES_DIR / key / filename
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load {path}")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    try:
        spec.loader.exec_module(mod)
    except BaseException:
        sys.modules.pop(name, None)
        raise
    return mod


# ── agent half ─────────────────────────────────────────────────────────────────────────────────

def register_agent_features(ctx) -> None:
    """Start every enabled feature that has an agent half, each behind its own boundary."""
    for feature in FEATURES.values():
        if not feature.agent:
            continue
        try:
            on = ctx.get_config(feature.key, True) is not False
        except Exception:
            on = True
        if not on:
            set_status(feature.key, "agent", "disabled")
            continue
        guarded(feature.key, "agent", lambda f=feature: load_feature_module(f.key, "agent.py").register(ctx))
    for key, link_name in PROVIDER_LINKS.items():
        guarded(key, "link", lambda k=key, n=link_name: ensure_provider_link(feature=k, link_name=n))


# ── native provider links ──────────────────────────────────────────────────────────────────────
# Model providers are discovered from ``<home>/plugins/model-providers/<name>/`` by a different
# loader than ordinary plugins. The provider code lives in this package; a symlink there points
# at it, so each provider keeps its id while t3code stays one package.

PROVIDER_LINKS = {"claude_cli": "claude-cli", "muse_code": "muse-code", "antigravity": "antigravity"}
PROVIDER_LINK_NAME = "claude-cli"


def ensure_provider_link(home: Optional[Path] = None, *, feature: str = "claude_cli",
                         link_name: str = PROVIDER_LINK_NAME) -> None:
    """Create ``<home>/plugins/model-providers/<link_name>`` → ``features/<feature>`` when missing.

    Leaves anything that already exists alone (a real directory is the user's own install).
    """
    if home is None:
        from hermes_constants import get_hermes_home
        home = Path(get_hermes_home())
    link = Path(home) / "plugins" / "model-providers" / link_name
    target = FEATURES_DIR / feature
    if link.is_symlink():
        if link.resolve() == target.resolve():
            return
        if link.exists():
            return  # points at another live install; not ours to replace
        link.unlink()  # dangling (e.g. an old plugin folder that was removed)
    elif link.exists():
        return
    link.parent.mkdir(parents=True, exist_ok=True)
    os.symlink(target, link, target_is_directory=True)
    logger.info("t3code: linked %s provider at %s", FEATURES[feature].label, link)
