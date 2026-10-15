"""The active profile's SOUL.md for native agents.

Hermes puts SOUL.md in the system prompt of its own API-model turns. Native agents (Claude CLI,
Muse Code, Antigravity) run *instead of* the Hermes prompt, so each provider has to deliver it
itself. This module only resolves it: which profile, what text, a content hash. Delivery is the
provider's job (Claude: system-prompt append; Muse / Antigravity: a leading block, see
``native_turns.TurnRuntime.identity_block``).

Everything here fails open: a missing or unreadable SOUL.md means "no identity", never a failed turn.
"""

from __future__ import annotations

import hashlib
import logging
import os
from pathlib import Path
from typing import Mapping, Optional

log = logging.getLogger(__name__)

SETTING = "inject_soul"
MAX_CHARS = 20_000  # only used when Hermes' own loader is unavailable


def enabled() -> bool:
    """``plugins.entries.t3code.settings.inject_soul`` of the active profile; on unless set off."""
    try:
        from hermes_cli.config import load_config_readonly
        entries = ((load_config_readonly() or {}).get("plugins") or {}).get("entries") or {}
        value = ((entries.get("t3code") or {}).get("settings") or {}).get(SETTING, True)
    except Exception:
        return True
    if isinstance(value, str):
        return value.strip().lower() not in ("false", "0", "no", "off")
    return bool(value)


def profile_home(environment: Optional[Mapping[str, str]] = None) -> Optional[Path]:
    """The profile this agent works for.

    ``environment`` is the subprocess env Hermes built for the turn; its ``HERMES_HOME`` is the
    context-local profile override, so it wins over the process-wide default (which is the launch
    profile, the wrong SOUL.md on a thread that lost the override).
    """
    raw = (environment or {}).get("HERMES_HOME") or ""
    if not raw:
        try:
            from hermes_constants import get_hermes_home
            raw = str(get_hermes_home())
        except Exception:
            raw = os.environ.get("HERMES_HOME") or ""
    return Path(raw).expanduser() if raw else None


def profile_name(home: Path) -> str:
    return home.name if home.parent.name == "profiles" else "default"


def _read(home: Path) -> str:
    """Hermes' own loader (injection scan, size cap, distribution trust), else the raw file."""
    try:
        from agent.prompt_builder import load_soul_md
        return (load_soul_md(home_override=home) or "").strip()
    except Exception as exc:
        log.debug("Hermes SOUL.md loader unavailable (%s); reading the file directly", exc)
    try:
        return (home / "SOUL.md").read_text(encoding="utf-8-sig").strip()[:MAX_CHARS]
    except OSError:
        return ""


def resolve(environment: Optional[Mapping[str, str]] = None) -> Optional[dict]:
    """``{"text", "hash", "profile"}`` for the active profile's SOUL.md, or None (off, missing, empty)."""
    try:
        if not enabled():
            return None
        home = profile_home(environment)
        if home is None:
            return None
        soul = _read(home)
        if not soul:
            return None
        name = profile_name(home)
        text = (f'<profile-identity profile="{name}" source="SOUL.md">\n'
                "This is your identity and standing instructions from the active Hermes profile. "
                "Hermes injects it because this agent does not receive the Hermes system prompt. "
                "Follow it for the whole conversation, together with this agent's own rules and tools. "
                "Do not mention this block unless asked.\n\n"
                f"{soul}\n</profile-identity>")
        return {"text": text, "hash": hashlib.sha256(soul.encode()).hexdigest()[:16], "profile": name}
    except Exception:
        log.warning("Could not load the profile SOUL.md; continuing without it", exc_info=True)
        return None
