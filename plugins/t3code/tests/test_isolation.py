"""T3 Code isolation: one feature failing or switched off leaves the others working.

Covers the Python entry points — REST mount (dashboard/plugin_api.py), agent registration
(__init__.py → shared/features.py), the Claude CLI provider gate and its discovery link.
The Desktop half has its own suite: node plugins/t3code/tests/desktop/isolation.mjs

Run: HERMES_HOME=$(mktemp -d) <python with pytest + Hermes deps> -m pytest plugins/t3code -q
"""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
import t3code_shared.features as features  # noqa: E402  (package registered by conftest)


def _import(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def fresh_features(monkeypatch):
    """Forget loaded feature modules and status so each test mounts from scratch."""
    for name in [n for n in sys.modules if n.startswith("t3code_") and n != "t3code_shared" and not n.startswith("t3code_shared.")]:
        monkeypatch.delitem(sys.modules, name)
    monkeypatch.setattr(features, "_status", {})
    return features


def _client(toggles=None, broken=()):
    real = features.load_feature_module

    def load(key, filename):
        if key in broken:
            raise RuntimeError(f"{key} is broken")
        return real(key, filename)

    features.load_feature_module = load
    try:
        api = _import(ROOT / "dashboard" / "plugin_api.py", "t3code_plugin_api_test")
    finally:
        features.load_feature_module = real
    state = {"toggles": {k: True for k in features.FEATURES} | (toggles or {})}
    features.current_toggles = lambda: dict(state["toggles"])
    app = FastAPI()
    app.include_router(api.router, prefix="/api/plugins/t3code")
    return TestClient(app), state


@pytest.fixture()
def restore_toggles():
    real = features.current_toggles
    yield
    features.current_toggles = real


# ── REST backend ────────────────────────────────────────────────────────────────────────────

def test_every_feature_mounts_under_its_own_prefix(fresh_features, restore_toggles, hermes_home):
    client, _ = _client()
    assert client.get("/api/plugins/t3code/inbox/lifecycle").json() == {"records": {}}
    assert client.get("/api/plugins/t3code/inbox/interrupted").status_code == 200
    body = client.get("/api/plugins/t3code/features").json()
    assert [f["key"] for f in body["features"]] == ["inbox", "usage", "orchestrator", "claude_cli", "muse_code", "antigravity"]
    assert all(f["enabled"] for f in body["features"])
    assert body["features"][0]["status"]["api"]["state"] == "running"


@pytest.mark.parametrize("broken", ["inbox", "usage", "orchestrator"])
def test_a_feature_that_fails_to_import_answers_503_and_the_rest_still_mount(broken, fresh_features, restore_toggles, hermes_home):
    client, _ = _client(broken={broken})
    res = client.get(f"/api/plugins/t3code/{broken}/anything")
    assert res.status_code == 503
    assert res.json()["detail"] == {"error": "feature_failed", "feature": broken, "message": f"RuntimeError: {broken} is broken"}
    if broken != "inbox":
        assert client.get("/api/plugins/t3code/inbox/lifecycle").status_code == 200
    else:
        assert client.get("/api/plugins/t3code/features").status_code == 200
    status = {f["key"]: f["status"] for f in client.get("/api/plugins/t3code/features").json()["features"]}
    assert status[broken]["api"]["state"] == "failed"
    assert all(status[k]["api"]["state"] == "running" for k in ("inbox", "usage", "orchestrator") if k != broken)


def test_a_switched_off_feature_answers_404_live_and_comes_back_when_switched_on(fresh_features, restore_toggles, hermes_home):
    client, state = _client()
    assert client.get("/api/plugins/t3code/inbox/lifecycle").status_code == 200
    state["toggles"]["inbox"] = False
    res = client.get("/api/plugins/t3code/inbox/lifecycle")
    assert res.status_code == 404 and res.json()["detail"]["error"] == "feature_disabled"
    assert client.get("/api/plugins/t3code/usage/limits").status_code != 404  # usage unaffected
    state["toggles"]["inbox"] = True
    assert client.get("/api/plugins/t3code/inbox/lifecycle").status_code == 200


def test_post_features_writes_through_the_plugin_settings_writer(fresh_features, restore_toggles, hermes_home, monkeypatch):
    client, _ = _client()
    written = {}
    import hermes_cli.plugins_settings as ps
    monkeypatch.setattr(ps, "save_plugin_settings", lambda pid, d, values: written.update({pid: values}) or list(values))
    assert client.post("/api/plugins/t3code/features", json={"usage": False}).status_code == 200
    assert written == {"t3code": {"usage": False}}
    assert client.post("/api/plugins/t3code/features", json={"usage": "no"}).status_code == 400
    assert client.post("/api/plugins/t3code/features", json={"nope": True}).status_code == 400


def test_the_settings_schema_declares_one_boolean_toggle_per_feature():
    import yaml
    schema = yaml.safe_load((ROOT / "plugin.yaml").read_text())["config_schema"]
    for key in features.FEATURES:
        assert schema[key]["type"] == "boolean" and schema[key]["default"] is True
        assert "." not in key  # the Hermes settings form reads values by the literal key


# ── agent half ──────────────────────────────────────────────────────────────────────────────

class Ctx:
    def __init__(self, settings=None):
        self.settings = settings or {}
        self.tools, self.hooks, self.commands = [], [], []

    def get_config(self, key, default=None):
        return self.settings.get(key, default)

    def register_tool(self, **kw):
        self.tools.append(kw["name"])

    def register_hook(self, name, fn):
        self.hooks.append(name)

    def register_command(self, name, **kw):
        self.commands.append(name)


def _entry():
    return _import(ROOT / "__init__.py", "t3code_entry_test")


def test_agent_register_survives_a_failing_feature(fresh_features, hermes_home, monkeypatch):
    monkeypatch.setattr(features, "load_feature_module", lambda key, filename: (_ for _ in ()).throw(ImportError("orchestrator boom")))
    monkeypatch.setattr(features, "ensure_provider_link", lambda home=None, **_: None)
    _entry().register(Ctx())  # must not raise
    snap = features.status_snapshot()
    assert snap["orchestrator"]["agent"]["state"] == "failed"
    assert "orchestrator boom" in snap["orchestrator"]["agent"]["error"]
    assert all(snap[k]["link"]["state"] == "running" for k in ("claude_cli", "muse_code", "antigravity"))


def test_agent_register_skips_a_switched_off_feature(fresh_features, hermes_home, monkeypatch):
    loaded = []
    monkeypatch.setattr(features, "load_feature_module", lambda key, filename: loaded.append(key))
    monkeypatch.setattr(features, "ensure_provider_link", lambda home=None, **_: None)
    _entry().register(Ctx({"orchestrator": False}))
    assert loaded == []
    assert features.status_snapshot()["orchestrator"]["agent"]["state"] == "disabled"


def test_a_failing_provider_link_does_not_stop_the_orchestrator(fresh_features, hermes_home, monkeypatch):
    class Agent:
        @staticmethod
        def register(ctx):
            ctx.register_tool(name="t3_delegate")

    monkeypatch.setattr(features, "load_feature_module", lambda key, filename: Agent)
    monkeypatch.setattr(features, "ensure_provider_link", lambda home=None, **_: (_ for _ in ()).throw(OSError("read-only")))
    ctx = Ctx()
    _entry().register(ctx)
    assert ctx.tools == ["t3_delegate"]
    snap = features.status_snapshot()
    assert snap["orchestrator"]["agent"]["state"] == "running"
    assert snap["claude_cli"]["link"]["state"] == "failed"


# ── Claude CLI provider gate + link ─────────────────────────────────────────────────────────

def _config(home: Path, text: str) -> Path:
    (home / "config.yaml").write_text(text)
    return home


@pytest.mark.parametrize("text,expected", [
    ("plugins:\n  enabled: [t3code]\n", True),
    ("plugins:\n  enabled: [t3code]\n  entries:\n    t3code:\n      settings:\n        claude_cli: false\n", False),
    ("plugins:\n  enabled: [other]\n", False),
    ("plugins:\n  enabled: [t3code]\n  disabled: [t3code]\n", False),
    (": : not yaml [", True),  # unreadable config fails open
])
def test_provider_gate(tmp_path, text, expected):
    assert features.feature_enabled_in_home("claude_cli", _config(tmp_path, text)) is expected


def test_provider_link_is_created_once_and_never_replaces_a_real_install(tmp_path):
    features.ensure_provider_link(tmp_path)
    link = tmp_path / "plugins" / "model-providers" / "claude-cli"
    assert link.is_symlink() and link.resolve() == (ROOT / "features" / "claude_cli").resolve()
    features.ensure_provider_link(tmp_path)  # idempotent
    assert link.is_symlink()

    other = tmp_path / "other"
    other.mkdir()
    (other / "plugins" / "model-providers").mkdir(parents=True)
    real = other / "plugins" / "model-providers" / "claude-cli"
    real.mkdir()
    features.ensure_provider_link(other)
    assert real.is_dir() and not real.is_symlink()

    dangling = tmp_path / "dangling"
    (dangling / "plugins" / "model-providers").mkdir(parents=True)
    os.symlink(tmp_path / "gone", dangling / "plugins" / "model-providers" / "claude-cli")
    features.ensure_provider_link(dangling)
    assert (dangling / "plugins" / "model-providers" / "claude-cli").resolve() == (ROOT / "features" / "claude_cli").resolve()


@pytest.mark.parametrize("feature,link_name", [("muse_code", "muse-code"), ("antigravity", "antigravity")])
def test_every_native_provider_gets_its_own_link(tmp_path, feature, link_name):
    features.ensure_provider_link(tmp_path, feature=feature, link_name=link_name)
    link = tmp_path / "plugins" / "model-providers" / link_name
    assert link.resolve() == (ROOT / "features" / feature).resolve()


def test_claude_cli_settings_prefer_t3code_and_fall_back_to_the_old_entry(monkeypatch):
    opts = _import(ROOT / "features" / "claude_cli" / "options.py", "t3code_claude_options_test")
    cfg = {"plugins": {"entries": {
        "claude-cli": {"settings": {"executable": "/old/claude", "permission_mode": "plan"}},
        "t3code": {"settings": {"claude_cli": True, "claude_executable": "/new/claude"}},
    }}}
    import hermes_cli.config as hc
    monkeypatch.setattr(hc, "load_config_readonly", lambda: cfg)
    assert opts.settings_for_profile() == {"executable": "/new/claude", "permission_mode": "plan"}
