"""Account-owned model discovery through a zero-inference SDK handshake."""

from __future__ import annotations

import asyncio
import concurrent.futures
import json
import subprocess
import tempfile
import threading
import time
from pathlib import Path

from .options import executable, native_options, settings_for_profile
from .model_selection import model_rows

_cache: dict = {}
_lock = threading.Lock()


def setup_status() -> dict:
    settings = settings_for_profile()
    try:
        binary = executable(settings)
    except FileNotFoundError as exc:
        return {"available": False, "logged_in": False, "detail": str(exc), "login_command": "claude auth login"}
    from tools.environments.local import hermes_subprocess_env
    env = hermes_subprocess_env(inherit_credentials=True)
    if settings.get("config_directory"):
        env["CLAUDE_CONFIG_DIR"] = str(Path(settings["config_directory"]).expanduser().resolve())
    try:
        done = subprocess.run([binary, "auth", "status"], capture_output=True, text=True, env=env, timeout=10, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"available": True, "logged_in": False, "detail": str(exc), "login_command": "claude auth login"}
    try:
        info = json.loads(done.stdout)
    except ValueError:
        info = {}
    logged_in = bool(info.get("loggedIn"))
    return {"available": True, "logged_in": logged_in, "plan": info.get("subscriptionType", ""),
            "detail": "Native Claude login" if logged_in else "Run claude auth login before selecting this runtime.",
            "login_command": "claude auth login"}


async def _probe(settings, environment, timeout):
    from claude_agent_sdk import ClaudeSDKClient
    with tempfile.TemporaryDirectory(prefix="hermes-claude-catalog-") as cwd:
        options = native_options(settings, environment, cwd, {}, probe=True)
        async with asyncio.timeout(timeout):
            async with ClaudeSDKClient(options) as client:
                info = await client.get_server_info() or {}
                return model_rows(info.get("models", []))


def discover_models(*, timeout=25):
    from hermes_constants import get_hermes_home
    from tools.environments.local import hermes_subprocess_env
    settings = settings_for_profile()
    key = (str(get_hermes_home()), json.dumps(settings, sort_keys=True))
    with _lock:
        prior = _cache.get(key)
        if prior and time.monotonic() - prior[0] < 60:
            return prior[1]
    # Picker discovery can run inside an existing event loop; own the probe task in a separate thread.
    environment = hermes_subprocess_env(inherit_credentials=True)
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
        try:
            rows = executor.submit(lambda: asyncio.run(_probe(settings, environment, timeout))).result(timeout + 5)
        except (TimeoutError, OSError, RuntimeError, ImportError):
            return None
    with _lock:
        _cache[key] = (time.monotonic(), rows)
    return rows or None
