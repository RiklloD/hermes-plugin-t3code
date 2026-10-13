"""Muse Code settings, status and model discovery (T3 Code ``status.ts`` + ``modelCatalog.ts``).

Discovery runs a read-only ``muse serve`` (no shell, no writes, no session log) and asks
``model/list``. MSP has no auth signal and ``model/list`` answers from Muse's cache even when
logged out, so auth failures surface on the first turn instead.
"""

from __future__ import annotations

import subprocess
import tempfile
import threading
import time
from pathlib import Path

from . import protocol

_cache: dict = {}
_lock = threading.Lock()
LOGIN_COMMAND = "muse login"


def settings_for_profile() -> dict:
    """``plugins.entries.t3code.settings.muse_<key>`` (``muse_code`` is the feature switch)."""
    from t3code_shared.features import provider_settings
    return provider_settings("muse_", toggle="muse_code")


def executable(settings: dict) -> str:
    from hermes_platform.resolver import locate_command
    resolved = locate_command(str(settings.get("binary_path") or "muse"),
                              known_dirs=(str(Path.home() / ".local/bin"), "/opt/homebrew/bin", "/usr/local/bin"))
    if not resolved.found:
        raise FileNotFoundError("Muse Code CLI (`muse`) was not found. Install Muse Code and run `muse login`.")
    return resolved.command[0]


def setup_status() -> dict:
    settings = settings_for_profile()
    try:
        binary = executable(settings)
    except FileNotFoundError as exc:
        return {"available": False, "logged_in": False, "detail": str(exc), "login_command": LOGIN_COMMAND}
    from tools.environments.local import hermes_subprocess_env
    try:
        done = subprocess.run([binary, "--version"], capture_output=True, text=True, timeout=10, check=False,
                              env=protocol.muse_environment(hermes_subprocess_env(inherit_credentials=True)))
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"available": True, "logged_in": False, "detail": f"Muse Code CLI failed to run: {exc}",
                "login_command": LOGIN_COMMAND}
    version = protocol.parse_version(f"{done.stdout}\n{done.stderr}")
    if done.returncode != 0:
        return {"available": True, "logged_in": False, "detail": "Muse Code CLI is installed but failed to run.",
                "login_command": LOGIN_COMMAND}
    rows = discover_models(timeout=12)
    return {"available": True, "logged_in": bool(rows), "version": version or "",
            "detail": f"Muse Code {version or ''}".strip() if rows else
            "Muse Code returned no models. Run `muse login` and refresh.",
            "login_command": LOGIN_COMMAND}


def _probe(binary: str, environment: dict, timeout: float) -> list[dict]:
    from t3code_shared.stdio_rpc import StdioRpc
    with tempfile.TemporaryDirectory(prefix="hermes-muse-catalog-") as cwd:
        rpc = StdioRpc([binary, *protocol.serve_args(read_only=True)], cwd=cwd, env=environment,
                       name="Muse Code", on_notification=lambda *_: None,
                       on_request=lambda request_id, *_: rpc.respond(request_id, {}))
        try:
            init = rpc.request("initialize", protocol.initialize_params(read_only=True), timeout=min(timeout, 8))
            if ((init or {}).get("schema") or {}).get("version") != 1:
                raise RuntimeError("Muse SDK returned an unsupported protocol envelope version.")
            rpc.notify("initialized")
            return protocol.model_rows(rpc.request("model/list", {}, timeout=timeout) or {})
        finally:
            rpc.close(5)


def discover_models(*, timeout=12):
    from hermes_constants import get_hermes_home
    from tools.environments.local import hermes_subprocess_env
    settings = settings_for_profile()
    key = (str(get_hermes_home()), str(settings.get("binary_path") or ""))
    with _lock:
        prior = _cache.get(key)
        if prior and time.monotonic() - prior[0] < 60:
            return prior[1]
    try:
        environment = protocol.muse_environment(hermes_subprocess_env(inherit_credentials=True))
        rows = _probe(executable(settings), environment, float(timeout))
    except Exception:
        return None
    with _lock:
        _cache[key] = (time.monotonic(), rows)
    return rows or None


def cached_row(model: str):
    for _at, rows in list(_cache.values()):
        for row in rows or []:
            if row["id"] == model:
                return row
    return None


def default_model():
    for _at, rows in list(_cache.values()):
        for row in rows or []:
            if row["is_default"]:
                return row["id"]
    return None
