"""Antigravity settings, sign-in, status and models (T3 Code ``AntigravityDriver.ts``,
``AntigravityAuth.ts``, ``AntigravityProvider.ts``).

As in T3, the status check never spawns the agent (a PyInstaller bundle that unpacks ~1 GB per
launch). Models come from the account: every session writes the catalog it reports to the profile,
and discovery only spawns a probe when no catalog is cached yet and the profile is signed in.
"""

from __future__ import annotations

import json
import sys
import tempfile
import time
from pathlib import Path

from . import protocol, release

CATALOG_TTL = 6 * 3600
CONTEXT_WINDOW = 1_048_576


def settings_for_profile() -> dict:
    """``plugins.entries.t3code.settings.antigravity_<key>`` (``antigravity`` is the feature switch)."""
    from t3code_shared.features import provider_settings
    return provider_settings("antigravity_", toggle="antigravity")


def profile_dir(settings: dict) -> Path:
    """GEMINI_HOME for the agent. One Google sign-in shared by every Hermes profile (like one T3 instance)."""
    if settings.get("profile_directory"):
        return Path(str(settings["profile_directory"])).expanduser().resolve()
    from t3code_shared.hermes_data import hermes_root
    return hermes_root() / "plugin-data" / "t3code" / "antigravity" / "profile"


def runtime(settings: dict) -> dict:
    found = release.resolve(str(settings.get("binary_path") or ""))
    if not found:
        raise FileNotFoundError(f"The Antigravity runtime is not installed. Run: {install_command()}")
    return found


def _entry() -> str:
    return f"{sys.executable} {Path(__file__).with_name('setup.py')}"


def install_command() -> str:
    return f"{_entry()} install"


def login_command() -> str:
    return f"{_entry()} login"


def signed_in(settings: dict) -> bool:
    auth = protocol.auth_config(settings)
    if not protocol.uses_browser(auth["method"]):
        return bool(auth["api_key"] or (auth["gcp_project"] and auth["gcp_location"]))
    # Existence only here; Usage ▸ Limits reads it like T3 does, keeping the access token in memory.
    return (profile_dir(settings) / "antigravity-acp" / "acp_token.json").is_file()


def setup_status() -> dict:
    settings = settings_for_profile()
    try:
        runtime(settings)
    except FileNotFoundError as exc:
        return {"available": False, "logged_in": False, "detail": str(exc), "login_command": install_command()}
    if not signed_in(settings):
        return {"available": True, "logged_in": False, "login_command": login_command(),
                "detail": "Sign in to Antigravity with your Google account before selecting this runtime."}
    return {"available": True, "logged_in": True, "login_command": login_command(),
            "detail": f"Antigravity ({protocol.auth_config(settings)['method']})"}


# ── model catalog ────────────────────────────────────────────────────────────────────────────────

def _catalog_path(settings: dict) -> Path:
    return profile_dir(settings) / "antigravity-acp" / "hermes-models.json"


def remember_models(settings: dict, config_options: list) -> None:
    rows = [{"id": o["value"], "label": o.get("name") or o["value"]} for o in protocol.model_options(config_options)
            if o.get("value")]
    if rows:
        path = _catalog_path(settings)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"at": time.time(), "current": protocol.current_model(config_options),
                                    "models": rows}))


def cached_models(settings: dict):
    try:
        return json.loads(_catalog_path(settings).read_text())
    except (OSError, ValueError):
        return None


class Host:
    """One disposable agent process: initialize → authenticate → session (login, probes)."""

    def __init__(self, settings: dict, cwd: str, on_status=print, open_browser=True):
        from t3code_shared.stdio_rpc import StdioRpc
        from tools.environments.local import hermes_subprocess_env
        self.settings, self.auth = settings, protocol.auth_config(settings)
        profile = profile_dir(settings)
        protocol.prepare_profile(profile, self.auth)
        found = runtime(settings)

        def stderr(line: str):
            if line.startswith(protocol.AUTH_URL_PREFIX):
                url = line[len(protocol.AUTH_URL_PREFIX):].strip()
                on_status(f"Sign in to Antigravity in your browser: {url}")
                if open_browser and url.startswith("https://accounts.google.com/"):
                    import webbrowser
                    webbrowser.open(url)
        self.rpc = StdioRpc(protocol.argv(found), cwd=cwd, name="Antigravity", on_notification=lambda *_: None,
                            env=protocol.environment(hermes_subprocess_env(inherit_credentials=True), profile, found, self.auth),
                            on_request=lambda request_id, *_: self.rpc.respond(
                                request_id, {"outcome": {"outcome": "cancelled"}}),
                            on_stderr=stderr)

    def start(self, auth_timeout: float) -> dict:
        self.rpc.request("initialize", initialize_params(file_system=False), timeout=60)
        # The configured method is authoritative: Antigravity accepts methods it does not list.
        self.rpc.request("authenticate", {"methodId": self.auth["method"]}, timeout=auth_timeout)
        with tempfile.TemporaryDirectory(prefix="hermes-antigravity-") as cwd:
            created = self.rpc.request("session/new", {"cwd": cwd, "mcpServers": []}, timeout=60) or {}
        remember_models(self.settings, created.get("configOptions") or [])
        return created

    def close(self):
        self.rpc.close(5)


def initialize_params(*, file_system: bool) -> dict:
    return {"protocolVersion": 2, "clientInfo": {"name": "hermes-t3code", "title": "Hermes · T3 Code", "version": "1"},
            "clientCapabilities": {"fs": {"readTextFile": file_system, "writeTextFile": file_system}, "terminal": False}}


def t3_cached_models():
    """T3 Code's own provider-status cache for the same Google account (``~/.t3/caches/antigravity.json``).

    Used only until Hermes's own session has reported a catalog, so the picker lists the account's
    models before the first Antigravity chat."""
    try:
        status = json.loads((Path.home() / ".t3" / "caches" / "antigravity.json").read_text())
    except (OSError, ValueError):
        return None
    models = [m for m in status.get("models") or [] if isinstance(m, dict) and m.get("slug") and not m.get("isCustom")]
    if not models:
        return None
    current = next((m["slug"] for m in models if m.get("isDefault")), models[0]["slug"])
    return {"at": 0, "current": current, "source": "t3code",
            "models": [{"id": m["slug"], "label": m.get("name") or m["slug"]} for m in models]}


def discover_models(*, timeout=90):
    settings = settings_for_profile()
    cached = cached_models(settings)
    if cached and time.time() - cached.get("at", 0) < CATALOG_TTL:
        return _rows(cached)
    if signed_in(settings):
        try:
            with tempfile.TemporaryDirectory(prefix="hermes-antigravity-probe-") as cwd:
                host = Host(settings, cwd, on_status=lambda _text: None, open_browser=False)
                try:
                    # A probe never waits on a browser: a stale sign-in fails fast.
                    host.start(auth_timeout=min(30, timeout))
                finally:
                    host.close()
            cached = cached_models(settings) or cached
        except Exception:
            pass
    cached = cached or t3_cached_models()
    return _rows(cached) if cached else None


def _rows(cached: dict) -> list[dict]:
    current = cached.get("current")
    return [{"id": m["id"], "label": m["label"], "is_default": m["id"] == current, "context_window": CONTEXT_WINDOW}
            for m in cached.get("models") or []]


def login(timeout: float = 600) -> None:
    """Interactive sign-in. For Google methods the agent opens the browser; the link is printed too."""
    settings = settings_for_profile()
    with tempfile.TemporaryDirectory(prefix="hermes-antigravity-login-") as cwd:
        host = Host(settings, cwd)
        try:
            created = host.start(auth_timeout=timeout)
        finally:
            host.close()
    models = ", ".join(o.get("name", o.get("value", "")) for o in protocol.model_options(created.get("configOptions") or []))
    print(f"Antigravity is signed in. Models: {models or 'none reported'}")
