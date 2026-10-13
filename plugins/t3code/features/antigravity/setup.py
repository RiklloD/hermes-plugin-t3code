"""Antigravity setup from a terminal (what T3 Code's provider settings buttons do).

    <hermes python> setup.py install   download + verify the official ACP runtime
    <hermes python> setup.py login     sign in with the configured method (opens the browser)
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]  # plugins/t3code


def _package():
    for candidate in (HERE.parents[3] / "hermes-agent", Path.home() / ".hermes" / "hermes-agent"):
        if (candidate / "hermes_constants.py").is_file() and str(candidate) not in sys.path:
            sys.path.insert(0, str(candidate))
    spec = importlib.util.spec_from_file_location("_t3code_shared_boot", ROOT / "shared" / "__init__.py")
    boot = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(boot)
    boot.load(ROOT)
    spec = importlib.util.spec_from_file_location("t3code_antigravity_setup", HERE / "__init__.py",
                                                  submodule_search_locations=[str(HERE)])
    package = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = package  # submodules only; the provider registers in the backend
    return spec.name


def main(argv: list[str]) -> int:
    action = argv[1] if len(argv) > 1 else ""
    if action not in ("install", "login"):
        print(__doc__)
        return 2
    name = _package()
    if action == "install":
        importlib.import_module(f"{name}.release").install()
    else:
        importlib.import_module(f"{name}.catalog").login()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
