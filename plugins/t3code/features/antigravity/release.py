"""The official Antigravity ACP runtime: release table and managed install.

Copied from T3 Code's ``antigravityRelease.ts`` / ``AntigravityInstallation.ts``. The layout is
T3's (``<dir>/<platform>-<arch>/versions/<sha256>/`` + ``active.json``), so a runtime T3 Code
already downloaded (``~/.t3/tools/antigravity-acp``) is reused instead of fetching 100+ MB again.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import shutil
import stat
import sys
import tempfile
import urllib.request
import zipfile
from pathlib import Path
from typing import Optional

VERSION = "1.3.0"
_BASE = "https://dl.google.com/agy-extensions/releases"
# URLs come from the official registry. Hashes and sizes were checked by T3 Code on 2026-10-05.
# https://github.com/agentclientprotocol/registry/blob/dc55a34900fdd60e5e97c1cbd7825c5a1df673fc/antigravity-acp/agent.json
ASSETS = {
    "darwin-arm64": (f"{_BASE}/macos/agy-acp-server-1.3.0-darwin-arm64.zip",
                     "7cd97045f7b4fe81175a107cdf16f9c51484e3c78a5162cae415338bb6aa5b88", 111_456_962),
    "darwin-x64": (f"{_BASE}/macos/agy-acp-server-1.3.0-darwin-x86_64.zip",
                   "bb23956b89984bf5d354af2c3725e6c57f0cc1b7228e77a0e91c9c2bc1d47646", 117_245_544),
    "linux-x64": (f"{_BASE}/linux/agy-acp-server-1.3.0-linux-x86_64.zip",
                  "9fb60956af0a9d76220a4db91ca9ac88e2a2372ad68f985ab5fceace6b825b96", 333_727_150),
    "linux-arm64": (f"{_BASE}/linux/agy-acp-server-1.3.0-linux-arm64.zip",
                    "500b0bc0fb858e88f4df404d4cedf80bf9298c178291e39e383d6c50b111cbdf", 321_690_363),
    "win32-x64": (f"{_BASE}/windows/agy-acp-server-1.3.0-windows-x86_64.zip",
                  "65215e0688681fa3116e048a9eab27ef53af1bbd6f3da3f1c52bd4911d8b17f9", 124_509_787),
    "win32-arm64": (f"{_BASE}/windows/agy-acp-server-1.3.0-windows-arm64.zip",
                    "4a0f469720e9beb9438a979f543fdbfad5022ebe0992c052c590bd78b3144ca3", 124_654_803),
}
NAMES = ({"executable": "agy_acp_server.exe", "harness": "localharness_external.exe"} if os.name == "nt"
         else {"executable": "agy_acp_server.par", "harness": "localharness_external"})


def platform_key() -> str:
    system = {"darwin": "darwin", "linux": "linux", "win32": "win32"}.get(sys.platform, sys.platform)
    machine = platform.machine().lower()
    return f"{system}-{'arm64' if machine in ('arm64', 'aarch64') else 'x64' if machine in ('x86_64', 'amd64') else machine}"


def managed_dir() -> Path:
    try:
        from hermes_constants import get_default_hermes_root
        root = Path(get_default_hermes_root())
    except Exception:
        root = Path.home() / ".hermes"
    return root / "plugin-data" / "t3code" / "antigravity-acp"


def _active(directory: Path) -> Optional[dict]:
    base = directory / platform_key()
    try:
        release = json.loads((base / "active.json").read_text())["releaseId"]
    except (OSError, ValueError, KeyError, TypeError):
        return None
    version_dir = base / "versions" / str(release)
    executable, harness = version_dir / NAMES["executable"], version_dir / NAMES["harness"]
    if executable.is_file() and harness.is_file() and os.access(executable, os.X_OK):
        return {"executable": str(executable), "harness": str(harness), "release": release}
    return None


def resolve(binary_path: str = "") -> Optional[dict]:
    """``{executable, harness}``: an explicit binary path, then Hermes's install, then T3 Code's."""
    if binary_path:
        executable = Path(binary_path).expanduser()
        harness = executable.parent / NAMES["harness"]
        if executable.is_file() and harness.is_file():
            return {"executable": str(executable), "harness": str(harness), "release": "custom"}
        raise FileNotFoundError("The custom Antigravity executable or its localharness_external sibling is missing.")
    return _active(managed_dir()) or _active(Path.home() / ".t3" / "tools" / "antigravity-acp")


def install(progress=print) -> dict:
    """Download, verify (size + sha256) and extract the runtime into Hermes's managed directory."""
    key = platform_key()
    if key not in ASSETS:
        raise RuntimeError(f"Antigravity has no managed runtime for {key}. Set antigravity_binary_path instead.")
    url, sha256, size = ASSETS[key]
    base = managed_dir() / key
    destination = base / "versions" / sha256
    base.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=base, prefix="download-") as work:
        archive = Path(work) / "runtime.zip"
        digest, received = hashlib.sha256(), 0
        progress(f"Downloading Antigravity ACP {VERSION} ({size // 1_000_000} MB)…")
        with urllib.request.urlopen(url, timeout=60) as response, archive.open("wb") as out:
            while chunk := response.read(1 << 20):
                received += len(chunk)
                if received > size:
                    raise RuntimeError("The Antigravity download is larger than the published archive.")
                digest.update(chunk)
                out.write(chunk)
        if received != size or digest.hexdigest() != sha256:
            raise RuntimeError("The Antigravity download failed verification.")
        progress("Extracting the verified runtime…")
        staging = Path(work) / "extract"
        with zipfile.ZipFile(archive) as zf:
            for name in NAMES.values():
                member = next((m for m in zf.namelist() if m.rsplit("/", 1)[-1] == name), None)
                if member is None:
                    raise RuntimeError(f"The Antigravity archive has no {name}.")
                staging.mkdir(exist_ok=True)
                with zf.open(member) as src, (staging / name).open("wb") as dst:
                    shutil.copyfileobj(src, dst)
                (staging / name).chmod((staging / name).stat().st_mode | stat.S_IXUSR | stat.S_IXGRP)
        if destination.exists():
            shutil.rmtree(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        staging.rename(destination)
    tmp = base / "active.json.tmp"
    tmp.write_text(json.dumps({"releaseId": sha256}))
    tmp.replace(base / "active.json")
    progress(f"Installed Antigravity ACP {VERSION} in {destination}")
    return _active(managed_dir())


if __name__ == "__main__":
    install()
