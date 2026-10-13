"""pytest wiring for every T3 Code test (``features/*/tests`` and ``tests``).

Run all:  <python with pytest + Hermes deps> -m pytest plugins/t3code -q
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent

# The shared layer is imported as ``t3code_shared`` by every feature, exactly as at runtime.
_spec = importlib.util.spec_from_file_location("_t3code_shared_boot", ROOT / "shared" / "__init__.py")
_boot = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_boot)
_boot.load(ROOT)


def load_feature_file(feature: str, filename: str, name: str):
    """A fresh import of ``features/<feature>/<filename>`` (tests patch module globals)."""
    spec = importlib.util.spec_from_file_location(name, ROOT / "features" / feature / filename)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def hermes_home(tmp_path, monkeypatch):
    """A throwaway Hermes root; ``hermes_constants`` answers with it."""
    root = tmp_path / ".hermes"
    root.mkdir()
    stub = type(sys)("hermes_constants")
    stub.get_default_hermes_root = lambda: str(root)
    stub.get_hermes_home = lambda: str(root)
    monkeypatch.setitem(sys.modules, "hermes_constants", stub)
    return root
