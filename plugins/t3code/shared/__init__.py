"""Code every T3 Code feature may use. Imported as the package ``t3code_shared``.

Keep this layer small: anything here is a dependency of several features, so a bug here is the
one kind of bug that can take more than one feature down.

* ``features``    — feature table, toggles, status, guarded startup, feature module loading
* ``hermes_data`` — where Hermes keeps its state (root, per-profile ``state.db``)
* ``desktop/``    — the Desktop shell (feature isolation for the UI half; bundled, not Python)
"""

import importlib.util
import sys
from pathlib import Path


def load(root: Path):
    """Import ``<root>/shared`` as ``t3code_shared`` once per process and return it."""
    mod = sys.modules.get("t3code_shared")
    if mod is None:
        spec = importlib.util.spec_from_file_location(
            "t3code_shared", Path(root) / "shared" / "__init__.py",
            submodule_search_locations=[str(Path(root) / "shared")])
        mod = importlib.util.module_from_spec(spec)
        sys.modules["t3code_shared"] = mod
        spec.loader.exec_module(mod)
    return mod
