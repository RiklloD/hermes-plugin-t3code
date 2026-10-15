"""Account-owned model discovery through a zero-inference SDK handshake."""

from __future__ import annotations

import asyncio
import concurrent.futures
import json
import tempfile
import threading
import time

from .options import native_options, settings_for_profile, settings_for_account2, account_status
from .model_selection import model_rows

_cache: dict = {}
_lock = threading.Lock()


def setup_status() -> dict:
    return account_status(settings_for_profile())


def setup_status_account2() -> dict:
    return account_status(settings_for_account2())


async def _probe(settings, environment, timeout):
    from claude_agent_sdk import ClaudeSDKClient
    with tempfile.TemporaryDirectory(prefix="hermes-claude-catalog-") as cwd:
        options = native_options(settings, environment, cwd, {}, probe=True)
        async with asyncio.timeout(timeout):
            async with ClaudeSDKClient(options) as client:
                info = await client.get_server_info() or {}
                return model_rows(info.get("models", []))


def discover_models(*, timeout=25, settings=None):
    from hermes_constants import get_hermes_home
    from tools.environments.local import hermes_subprocess_env
    if settings is None:
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
