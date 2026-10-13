"""Muse Code + Antigravity for the Usage page: token history (Tokens / Cost) and live limits.

Antigravity is a port of T3 Code's ``antigravityUsageReader.ts`` (conversation SQLite databases whose
usage metadata is protobuf) and ``antigravityUsageLimits.ts`` (Google's quota summary for the
signed-in profile, with a memory-only access token). Muse Code logs every model call as a
``model_completed`` event in its session journals; its subscription windows are pushed live as MSP
``usage/changed`` and saved by the Muse Code runtime (``hermes_data.muse_usage_path``) (Muse keeps no copy on disk).
"""

from __future__ import annotations

import glob
import hashlib
import json
import os
import platform
import sqlite3
import sys
import threading
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

HOME = Path.home()
Record = Tuple[int, str, int, int, int, int, int, str, Optional[str], str]
_cache: Dict[str, Tuple[str, List[Tuple[Record, List[str]]]]] = {}
_cache_lock = threading.Lock()


def hermes_root() -> Path:
    from t3code_shared.hermes_data import hermes_root as root
    return root()


def _homes() -> List[Path]:
    root = hermes_root()
    homes = [root]
    if (root / "profiles").is_dir():
        homes += sorted(p for p in (root / "profiles").iterdir() if p.is_dir())
    return homes


# ─── Antigravity: protobuf usage metadata ───────────────────────────────────────────────────────

class _Bad(ValueError):
    pass


def _fields(data: bytes) -> Dict[int, list]:
    out: Dict[int, list] = {}
    i, n = 0, len(data)

    def varint() -> int:
        nonlocal i
        value, shift = 0, 0
        while shift < 70:
            if i >= n:
                raise _Bad("varint")
            byte = data[i]
            i += 1
            value |= (byte & 127) << shift
            if byte < 128:
                return value
            shift += 7
        raise _Bad("varint")

    while i < n:
        tag = varint()
        number, wire = tag >> 3, tag & 7
        if number == 0:
            raise _Bad("field")
        if wire == 0:
            value: Any = varint()
        elif wire in (1, 2, 5):
            length = varint() if wire == 2 else 8 if wire == 1 else 4
            if length > n - i:
                raise _Bad("truncated")
            value = data[i:i + length]
            i += length
            if wire != 2:
                continue
        else:
            raise _Bad("wire type")
        out.setdefault(number, []).append(value)
    return out


def _num(f: dict, k: int) -> int:
    v = (f.get(k) or [None])[0]
    return v if isinstance(v, int) else 0


def _bytes(f: dict, k: int) -> Optional[bytes]:
    v = (f.get(k) or [None])[0]
    return v if isinstance(v, (bytes, bytearray)) else None


def _nested(f: dict, k: int) -> dict:
    b = _bytes(f, k)
    return {} if b is None else _fields(b)


def _text(f: dict, k: int) -> str:
    b = _bytes(f, k)
    return "" if b is None else b.decode("utf-8").strip()


def _stamp(f: dict) -> Optional[int]:
    seconds = _num(f, 1)
    return seconds * 1000 + _num(f, 2) // 1_000_000 if seconds > 0 else None


MODEL_IDS = {
    246: "gemini-2.5-pro", 312: "gemini-2.5-flash", 313: "gemini-2.5-flash-thinking",
    329: "gemini-2.5-flash-thinking", 330: "gemini-2.5-flash-lite", 281: "claude-sonnet-4", 282: "claude-sonnet-4",
    290: "claude-opus-4", 291: "claude-opus-4", 333: "claude-sonnet-4-5", 334: "claude-sonnet-4-5",
    340: "claude-haiku-4-5", 341: "claude-haiku-4-5", 1026: "claude-opus-4-6", 1035: "claude-sonnet-4-6",
    1016: "gemini-3.1-pro", 1036: "gemini-3.1-pro", 1037: "gemini-3.1-pro", 1018: "gemini-3-flash-preview",
    1084: "gemini-3-flash-preview", 1047: "gemini-3-flash-preview",
}


def _model_name(name: str, model_id: int) -> str:
    import re
    if name:
        norm = re.sub(r"\s*\([^)]*\)\s*$", "", name.lower()).replace(" ", "-")
        if norm.startswith("claude-"):
            return re.sub(r"^claude-(4(?:\.\d+)?)-(sonnet|opus|haiku)", r"claude-\2-\1", norm).replace(".", "-")
        return norm
    return MODEL_IDS.get(model_id) or (f"antigravity-model-{model_id}" if model_id > 0 else "")


def _metadata(blob: bytes, step: bool) -> Tuple[str, Optional[int], List[dict]]:
    root = _fields(blob)
    if not step and _bytes(root, 1) is None:
        raise _Bad("generation metadata")
    data = root if step else _nested(root, 1)
    model = _nested(data, 24) if step else data
    usage = _bytes(data, 9 if step else 4)
    usages = [] if usage is None else [_fields(usage)]
    for retry in data.get(28 if step else 17) or []:
        if not isinstance(retry, (bytes, bytearray)):
            raise _Bad("retry")
        retry_usage = _bytes(_fields(retry), 2)
        if retry_usage is not None:
            usages.append(_fields(retry_usage))
    name = _model_name(_text(model, 12 if step else 19) or _text(model, 8 if step else 21), _num(model, 1 if step else 3))
    ts = (_stamp(_nested(data, 8)) or _stamp(_nested(data, 1))) if step else _stamp(_nested(_nested(data, 9), 4))
    return name, ts, usages


def _read_db(path: Path, session: str, fallback_ms: int) -> List[Tuple[Record, List[str]]]:
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=0.1)
    try:
        tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        if "gen_metadata" not in tables and "steps" not in tables:
            raise _Bad("no usage tables")
        gens = [(idx, _metadata(bytes(b), False)) for idx, b in
                con.execute("SELECT idx, data FROM gen_metadata ORDER BY idx")] if "gen_metadata" in tables else []
        traj = None
        if "trajectory_metadata_blob" in tables:
            for (b,) in con.execute("SELECT data FROM trajectory_metadata_blob"):
                traj = traj or _stamp(_nested(_fields(bytes(b)), 2))
        steps = [(idx, _metadata(bytes(b), True)) for idx, b in
                 con.execute("SELECT idx, metadata FROM steps WHERE metadata IS NOT NULL ORDER BY idx")] \
            if "steps" in tables else []
    finally:
        con.close()
    gen_models = {idx: meta[0] for idx, meta in gens}
    out: List[Tuple[Record, List[str]]] = []
    for source, entries in (("step", steps), ("generation", gens)):
        for index, (idx, (model, ts, usages)) in enumerate(entries):
            for u_index, u in enumerate(usages):
                output = max(_num(u, 3), _num(u, 9) + _num(u, 10))
                unc, cac, cw = _num(u, 2), _num(u, 5), _num(u, 4)
                if unc + cac + cw + output == 0:
                    continue
                keys = [f"antigravity:{k}:{_text(u, k)}" for k in (11, 12, 7) if _text(u, k)]
                name = (MODEL_IDS.get(_num(u, 1)) or model or (gen_models.get(idx, "") if source == "step" else "")
                        or _model_name("", _num(u, 1)) or "antigravity-unknown")
                record: Record = (ts or traj or fallback_ms, name, unc, cac, cw, output, min(output, _num(u, 9)),
                                  "standard", keys[0] if keys else f"antigravity:{session}:{source}:{index}:{u_index}",
                                  session)
                out.append((record, keys))
    return out


def antigravity_roots() -> List[Path]:
    """T3's default data roots, Hermes's Antigravity profiles and T3 Code's own instance profiles."""
    configured = [p.strip() for p in os.environ.get("ANTIGRAVITY_DATA_DIR", "").split(",") if p.strip()]
    roots = [Path(p).expanduser() for p in configured] or [
        *(HOME / ".gemini" / n for n in ("antigravity", "antigravity-cli", "antigravity-ide", "antigravity-backup")),
        HOME / ".config" / "antigravity"]
    roots += [d / "antigravity-acp" for d in profile_dirs()]
    roots += [Path(p) / "antigravity-acp" for p in glob.glob(str(HOME / ".t3" / "userdata" / "providers" / "antigravity" / "*"))]
    out, seen = [], set()
    for root in roots:
        conv = root / "conversations"
        target = (conv if conv.is_dir() else root).resolve()
        if target.is_dir() and target not in seen:
            seen.add(target)
            out.append(target)
    return out


def antigravity_records(cutoff_s: float) -> List[Record]:
    groups: List[Tuple[Record, List[str]]] = []
    for root in antigravity_roots():
        for dirpath, _dirs, files in os.walk(root):
            for name in sorted(files):
                if not name.endswith(".db"):
                    continue
                path = Path(dirpath) / name
                try:
                    st = path.stat()
                    wal = Path(f"{path}-wal")
                    wst = wal.stat() if wal.exists() else None
                except OSError:
                    continue
                if max(st.st_mtime, wst.st_mtime if wst else 0) < cutoff_s:
                    continue
                fingerprint = f"{st.st_size}:{st.st_mtime_ns}:{st.st_ctime_ns}/" + \
                              (f"{wst.st_size}:{wst.st_mtime_ns}" if wst else "-")
                key = str(path.resolve())
                with _cache_lock:
                    hit = _cache.get(key)
                if hit and hit[0] == fingerprint:
                    groups.extend(hit[1])
                    continue
                try:
                    recs = _read_db(path, path.stem, int(st.st_mtime * 1000))
                except (sqlite3.Error, _Bad, UnicodeDecodeError, OSError):
                    continue
                with _cache_lock:
                    _cache[key] = (fingerprint, recs)
                groups.extend(recs)
    # One generation can be stored in several databases (forks, backups): keep it once per identity.
    seen: set = set()
    out: List[Record] = []
    for record, keys in groups:
        if keys and any(k in seen for k in keys):
            continue
        seen.update(keys)
        out.append(record)
    return out


# ─── Muse Code: session journals ────────────────────────────────────────────────────────────────

def muse_roots() -> List[Path]:
    base = Path(os.environ.get("MUSE_HOME") or HOME / ".local" / "share" / "muse")
    return [base / "sessions"] if (base / "sessions").is_dir() else []


def _parse_muse(path: Path) -> List[Record]:
    out: List[Record] = []
    with path.open("r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            if '"model_completed"' not in line:
                continue
            try:
                row = json.loads(line)
            except ValueError:
                continue
            event = ((row.get("payload") or {}).get("event") or {})
            if event.get("kind") != "model_completed":
                continue
            u = event.get("usage") or {}
            cached = int(u.get("cache_read_tokens") or u.get("cached_tokens") or 0)
            written = int(u.get("cache_write_tokens") or 0)
            uncached = max(0, int(u.get("input_tokens") or 0) - cached - written)
            output = int(u.get("output_tokens") or 0)
            if uncached + cached + written + output == 0:
                continue
            ts = int(row.get("recorded_at") or 0) // 1000 or int(path.stat().st_mtime * 1000)
            out.append((ts, str(event.get("model") or "muse-unknown"), uncached, cached, written, output,
                        min(output, int(u.get("reasoning_tokens") or 0)), "standard", f"muse:{row.get('id')}",
                        str((row.get("stream") or {}).get("id") or path.parent.name)))
    return out


def muse_records(cutoff_s: float) -> List[Record]:
    out: List[Record] = []
    for root in muse_roots():
        for dirpath, _dirs, files in os.walk(root):
            if "session.jsonl" not in files:
                continue
            path = Path(dirpath) / "session.jsonl"
            try:
                st = path.stat()
            except OSError:
                continue
            if st.st_mtime < cutoff_s:
                continue
            fingerprint = f"{st.st_size}:{st.st_mtime_ns}"
            with _cache_lock:
                hit = _cache.get(str(path))
            if hit and hit[0] == fingerprint:
                out.extend(r for r, _ in hit[1])
                continue
            try:
                recs = _parse_muse(path)
            except OSError:
                continue
            with _cache_lock:
                _cache[str(path)] = (fingerprint, [(r, []) for r in recs])
            out.extend(recs)
    return out


# ─── limits ─────────────────────────────────────────────────────────────────────────────────────

def _iso_ms(ms: Any) -> Optional[str]:
    from datetime import datetime, timezone
    return datetime.fromtimestamp(ms / 1000, timezone.utc).isoformat() if isinstance(ms, (int, float)) and ms > 0 else None


def muse_limits(window_fn) -> Dict[str, Any]:
    try:
        from t3code_shared.hermes_data import muse_usage_path
        usage = json.loads(muse_usage_path().read_text())
    except (OSError, ValueError):
        return {"provider": "muse", "status": "unavailable",
                "message": "Muse Code reports its limits during a turn. Run one Muse Code chat to see them."}
    w, wk = usage.get("window") or {}, usage.get("weekly") or {}
    mins = int(w.get("windowDurationMins") or 300)
    windows = [window_fn("window", "Session" if mins < 1440 else f"{mins // 60}h", "session",
                         w.get("usedPercent"), _iso_ms(w.get("resetsAtMs")), mins)]
    if wk:
        windows.append(window_fn("weekly", "Weekly", "weekly", wk.get("usedPercent"), _iso_ms(wk.get("resetsAtMs")), 10080))
    now_ms = time.time() * 1000
    # A reset that already passed means the window restarted since the observation.
    for win, raw in zip(windows, (w, wk)):
        if isinstance(raw.get("resetsAtMs"), (int, float)) and raw["resetsAtMs"] < now_ms:
            win["usedPercent"], win["idle"] = 0.0, True
    return {"provider": "muse", "status": "ok", "plan": None, "windows": windows,
            "observedAt": _iso_ms(usage.get("observedAtMs"))}


_ENDPOINTS = ("https://daily-cloudcode-pa.googleapis.com", "https://cloudcode-pa.googleapis.com")
_token_cache: Dict[str, Tuple[str, float]] = {}


def profile_dirs() -> List[Path]:
    """Hermes Antigravity profiles (every Hermes profile's default location), first the active one."""
    dirs: List[Path] = []
    try:
        from hermes_constants import get_hermes_home
        dirs.append(Path(get_hermes_home()) / "plugin-data" / "t3code" / "antigravity" / "profile")
    except Exception:
        pass
    dirs += [h / "plugin-data" / "t3code" / "antigravity" / "profile" for h in _homes()]
    out: List[Path] = []
    for d in dirs:
        if d not in out and d.is_dir():
            out.append(d)
    return out


def _post(url: str, body: bytes, headers: dict) -> dict:
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read().decode("utf-8") or "{}")


def _quota_window(window_fn, id_: str, label: str, quota: dict, kind: str, mins: Optional[int]):
    fraction = quota.get("remainingFraction")
    if not isinstance(fraction, (int, float)):
        return None
    return window_fn(id_, label, kind, (1 - max(0.0, min(1.0, fraction))) * 100, quota.get("resetTime"), mins or 0)


def _summary_windows(window_fn, summary: dict) -> list:
    """The summary names the shared buckets; Claude and GPT consume one allowance."""
    groups = summary.get("groups") or []
    if not any(g.get("buckets") for g in groups):
        groups = [{"buckets": summary.get("buckets") or []}]
    windows = {}
    for group in groups:
        for bucket in group.get("buckets") or []:
            if bucket.get("disabled") or not bucket.get("bucketId"):
                continue
            bid = bucket["bucketId"]
            family = "Gemini" if bid.startswith("gemini-") else "Claude & GPT (shared)" if bid.startswith("3p-") \
                else (group.get("displayName") or "").strip() or bid
            weekly, session = bucket.get("window") == "weekly", bucket.get("window") == "5h"
            w = _quota_window(window_fn, bid, f"{family} · {'Weekly' if weekly else '5-hour' if session else (bucket.get('displayName') or 'Quota').strip()}",
                              bucket, "weekly" if weekly else "session" if session else "other",
                              10080 if weekly else 300 if session else None)
            if w:
                windows[bid] = w
    return list(windows.values())


def _model_windows(window_fn, models: dict) -> list:
    """Older accounts expose model quotas without a window length. Do not invent a reset cadence."""
    windows: Dict[str, dict] = {}
    for model_id, model in (models.get("models") or {}).items():
        provider = model.get("modelProvider")
        family = ("Gemini" if provider == "MODEL_PROVIDER_GOOGLE" or model_id.startswith("gemini-") else
                  "Claude" if provider == "MODEL_PROVIDER_ANTHROPIC" or model_id.startswith("claude-") else
                  "GPT" if provider == "MODEL_PROVIDER_OPENAI" or model_id.startswith("gpt-") else None)
        if not family:
            continue
        for suffix, quota in (("quota", model.get("quotaInfo")), ("weekly", model.get("weeklyQuotaInfo"))):
            if not isinstance(quota, dict):
                continue
            w = _quota_window(window_fn, f"{family.lower()}-{suffix}",
                              f"{family} · {'Weekly' if suffix == 'weekly' else 'Reported quota'}", quota,
                              "weekly" if suffix == "weekly" else "other", 10080 if suffix == "weekly" else None)
            if w and (w["id"] not in windows or w["usedPercent"] > windows[w["id"]]["usedPercent"]):
                windows[w["id"]] = w
    return list(windows.values())


def antigravity_limits(window_fn) -> Dict[str, Any]:
    """Google's quota for the first signed-in Antigravity profile (Hermes's own, then T3 Code's)."""
    candidates = [*profile_dirs(), *(Path(p) for p in sorted(glob.glob(str(HOME / ".t3" / "userdata" / "providers" / "antigravity" / "*"))))]
    token_path = next((d / "antigravity-acp" / "acp_token.json" for d in candidates
                       if (d / "antigravity-acp" / "acp_token.json").is_file()), None)
    if token_path is None:
        return {"provider": "antigravity", "status": "unavailable",
                "message": "Sign in with Google to see Antigravity limits."}
    token = json.loads(token_path.read_text())
    if not all(token.get(k) for k in ("client_id", "client_secret", "refresh_token")):
        return {"provider": "antigravity", "status": "unsupported",
                "message": "This Antigravity sign-in does not report limits."}
    fingerprint = hashlib.sha256(f"{token['client_id']}\0{token['refresh_token']}\0{token.get('project_id') or ''}".encode()).hexdigest()
    cached = _token_cache.get(fingerprint)
    if cached and cached[1] > time.time() + 60:
        access = cached[0]
    else:
        refreshed = _post("https://oauth2.googleapis.com/token", urllib.parse.urlencode({
            "client_id": token["client_id"], "client_secret": token["client_secret"],
            "refresh_token": token["refresh_token"], "grant_type": "refresh_token"}).encode(),
            {"Content-Type": "application/x-www-form-urlencoded"})
        access = refreshed["access_token"]
        _token_cache.clear()
        _token_cache[fingerprint] = (access, time.time() + float(refreshed.get("expires_in") or 0))
    system = {"darwin": "darwin", "win32": "windows"}.get(sys.platform, "linux")
    headers = {"Authorization": f"Bearer {access}", "Content-Type": "application/json",
               "User-Agent": f"antigravity/hub/1.0.0 (aidev_client; os_type={system}; arch={platform.machine()}; cl=963137146)"}
    body = json.dumps({"project": token["project_id"]} if token.get("project_id") else {}).encode()

    def fetch(operation, parse):
        for endpoint in _ENDPOINTS:
            try:
                windows = parse(_post(f"{endpoint}/v1internal:{operation}", body, headers))
            except Exception:
                continue
            if windows:
                return windows
        return []

    windows = fetch("retrieveUserQuotaSummary", lambda r: _summary_windows(window_fn, r)) or \
        fetch("fetchAvailableModels", lambda r: _model_windows(window_fn, r))
    if not windows:
        return {"provider": "antigravity", "status": "error", "message": "Antigravity did not return usable quota information."}
    source = "T3 Code's sign-in" if ".t3" in str(token_path) else None
    return {"provider": "antigravity", "status": "ok", "plan": None, "windows": windows, "account": source}


def billing_provider(provider: str, model: str) -> Optional[str]:
    """Pricing family for a native-agent model (subscription use; shown as API-equivalent)."""
    if provider not in ("antigravity", "muse"):
        return None
    m = model.lower()
    return "anthropic" if m.startswith("claude") else "openai" if m.startswith(("gpt", "o3", "o4")) else \
        "gemini" if m.startswith("gemini") else None
