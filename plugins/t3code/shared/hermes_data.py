"""Where Hermes keeps its state. Shared by the Inbox and Usage backends."""

from __future__ import annotations

from pathlib import Path
from typing import Any, List, Tuple


def hermes_root() -> Path:
    """The default Hermes home (profiles live under ``<root>/profiles``)."""
    try:
        from hermes_constants import get_default_hermes_root  # type: ignore
        return Path(get_default_hermes_root())
    except Exception:
        return Path.home() / ".hermes"


def muse_usage_path() -> Path:
    """Last Muse Code subscription usage the runtime saw (Muse keeps none on disk). Usage reads it."""
    return hermes_root() / "plugin-data" / "t3code" / "muse-usage.json"


def profile_dbs() -> List[Tuple[str, Path]]:
    """``[(profile, state.db)]`` for the default home and every profile that has a database."""
    root = hermes_root()
    dbs = [("default", root / "state.db")]
    profiles = root / "profiles"
    if profiles.is_dir():
        for child in sorted(profiles.iterdir()):
            if (child / "state.db").is_file():
                dbs.append((child.name, child / "state.db"))
    return [(n, p) for n, p in dbs if p.is_file()]


def positive_int(v: Any) -> int:
    return int(v) if isinstance(v, (int, float)) and v == v and v > 0 else 0
