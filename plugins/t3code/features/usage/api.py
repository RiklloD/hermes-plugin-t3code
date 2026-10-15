"""T3 Code · Usage backend: Claude Code + Codex + Z.ai + Antigravity + Muse Code usage for the Usage page.

A port of T3 Code's usage service (apps/server/src/usage) onto Hermes. It is the
only usage backend in T3 Code (the Inbox no longer carries a copy).

* Claude Code — every assistant record in ``~/.claude/projects/**/*.jsonl``.
  Claude writes one record per content block, each repeating the parent
  message's full ``usage``, so records are de-duplicated by
  ``message.id:requestId`` (first wins), exactly like T3 / ccusage. Hermes'
  Claude CLI runtime drives the real ``claude`` binary, so its turns land here.
* Codex — ``token_count`` events in ``~/.codex/sessions`` rollouts (real Codex
  CLI / app-server runs), PLUS Hermes' own ``openai-codex`` usage from every
  profile's ``state.db``: Hermes talks to the ChatGPT Codex backend directly,
  so those turns never produce a rollout file. The two sources are disjoint.
* Limits — the providers' own live answers: Claude's ``get_usage`` control
  request over stream-json, Codex's ``account/rateLimits/read`` over
  app-server, and Z.ai's Coding Plan quota API.

Costs are API-equivalent estimates priced with Hermes' pricing tables.

Routes (mounted at /api/plugins/t3code/usage/ by dashboard/plugin_api.py):
  GET /usage?days=N   tokens + cost, by provider / model / day (hour for N=1)
  GET /limits         live rate-limit windows per provider (cached 60 s)
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import sqlite3
import subprocess
import threading
import time
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from fastapi import APIRouter, Query

from t3code_shared.features import load_feature_module
from t3code_shared.hermes_data import positive_int as _int, profile_dbs as _profile_dbs

_native = load_feature_module("usage", "native.py")  # Muse Code + Antigravity

router = APIRouter()

HOME = Path.home()
CODEX_HOME = Path(os.environ.get("CODEX_HOME") or HOME / ".codex")
CODEX_ROOTS = [CODEX_HOME / "sessions", CODEX_HOME / "archived_sessions"]
FORK_COPY_MAX_GAP_MS = 1000
LIMITS_TTL_S = 60.0
PROBE_TIMEOUT_S = 25.0

# ─── transcript parsing (port of T3's usageTranscripts.ts) ──────────────────
# record: (ts_ms, model, uncached, cached, cache_write, output, reasoning, speed, dedupe, session)
Record = Tuple[int, str, int, int, int, int, int, str, Optional[str], str]


def _ts_ms(v: Any) -> Optional[int]:
    if not isinstance(v, str):
        return None
    try:
        return int(datetime.fromisoformat(v.replace("Z", "+00:00")).timestamp() * 1000)
    except ValueError:
        return None


def _parse_claude_file(path: Path) -> List[Record]:
    out: List[Record] = []
    with path.open("r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            if '"usage"' not in line:
                continue
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if not isinstance(rec, dict) or rec.get("type") != "assistant":
                continue
            msg = rec.get("message")
            if not isinstance(msg, dict):
                continue
            usage = msg.get("usage")
            model = msg.get("model")
            ts = _ts_ms(rec.get("timestamp"))
            if not isinstance(usage, dict) or not isinstance(model, str) or not model or ts is None:
                continue
            mid = msg.get("id") if isinstance(msg.get("id"), str) else None
            rid = rec.get("requestId") if isinstance(rec.get("requestId"), str) else None
            dedupe = None if mid is None and rid is None else f"{mid or ''}:{rid or ''}"
            out.append((
                ts, model,
                _int(usage.get("input_tokens")),
                _int(usage.get("cache_read_input_tokens")),
                _int(usage.get("cache_creation_input_tokens")),
                _int(usage.get("output_tokens")),
                0,
                "fast" if usage.get("speed") == "fast" else "standard",
                dedupe,
                rec.get("sessionId") if isinstance(rec.get("sessionId"), str) else "",
            ))
    return out


def _codex_speed(tier: Any) -> str:
    if tier in ("priority", "fast"):
        return "fast"
    if tier == "ultrafast":
        return "ultrafast"
    return "standard"


def _is_forked_meta(payload: dict) -> bool:
    if isinstance(payload.get("forked_from_id"), str):
        return True
    src = payload.get("source")
    sub = src.get("subagent") if isinstance(src, dict) else None
    spawn = sub.get("thread_spawn") if isinstance(sub, dict) else None
    return isinstance(spawn, dict) and isinstance(spawn.get("parent_thread_id"), str)


def _parse_codex_file(path: Path) -> List[Record]:
    out: List[Record] = []
    model, speed, session = "", "standard", ""
    last_sig: Optional[str] = None
    saw_meta = False
    suppress, anchor = False, 0
    with path.open("r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            if ('"token_count"' not in line and '"session_meta"' not in line
                    and '"turn_context"' not in line and '"thread_settings_applied"' not in line):
                continue
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if not isinstance(rec, dict):
                continue
            payload = rec.get("payload")
            if not isinstance(payload, dict):
                continue
            ptype = payload.get("type")
            if rec.get("type") == "session_meta":
                if saw_meta:
                    continue
                saw_meta = True
                sid = payload.get("id") or payload.get("session_id")
                if isinstance(sid, str):
                    session = sid
                mts = _ts_ms(rec.get("timestamp"))
                if mts is not None and _is_forked_meta(payload):
                    suppress, anchor = True, mts
                continue
            if rec.get("type") == "turn_context":
                if isinstance(payload.get("model"), str):
                    model = payload["model"]
                continue
            if ptype == "thread_settings_applied":
                st = payload.get("thread_settings")
                if isinstance(st, dict):
                    speed = _codex_speed(st.get("service_tier"))
                continue
            if ptype != "token_count":
                continue
            info = payload.get("info")
            last = info.get("last_token_usage") if isinstance(info, dict) else None
            if not isinstance(last, dict):
                continue
            ts = _ts_ms(rec.get("timestamp"))
            if ts is None or not model:
                continue
            sig = json.dumps(last, sort_keys=True)
            if sig == last_sig:
                continue
            last_sig = sig
            if suppress:
                if ts - anchor < FORK_COPY_MAX_GAP_MS:
                    anchor = ts
                    continue
                suppress = False
            inp = _int(last.get("input_tokens"))
            cached = _int(last.get("cached_input_tokens"))
            cwrite = _int(last.get("cache_write_input_tokens"))
            outp = _int(last.get("output_tokens"))
            uncached = max(0, inp - cached - cwrite)
            if uncached + cached + cwrite + outp == 0:
                continue
            out.append((ts, model, uncached, cached, cwrite, outp,
                        min(outp, _int(last.get("reasoning_output_tokens"))), speed, None, session))
    return out


# Parsed-file cache: path → (mtime_ns, size, records). Transcripts are append-
# only, so an unchanged (mtime, size) means the cached parse is still exact.
_file_cache: Dict[Tuple[str, str], Tuple[int, int, List[Record]]] = {}
_cache_lock = threading.Lock()


def _iter_files(roots: Iterable[Path], cutoff_s: float) -> Iterable[Path]:
    for root in roots:
        if not root.is_dir():
            continue
        for dirpath, _dirs, files in os.walk(root):
            for name in files:
                if not name.endswith(".jsonl"):
                    continue
                p = Path(dirpath) / name
                try:
                    if p.stat().st_mtime >= cutoff_s:
                        yield p
                except OSError:
                    continue


def _scan(provider: str, cutoff_s: float, roots: List[Path] = None) -> List[Record]:
    if roots is None:
        roots = _all_claude_roots() if provider == "claude" else CODEX_ROOTS
    parser = _parse_claude_file if provider == "claude" else _parse_codex_file
    records: List[Record] = []
    for path in _iter_files(roots, cutoff_s):
        try:
            st = path.stat()
        except OSError:
            continue
        key = (provider, str(path))
        with _cache_lock:
            hit = _file_cache.get(key)
        if hit and hit[0] == st.st_mtime_ns and hit[1] == st.st_size:
            recs = hit[2]
        else:
            try:
                recs = parser(path)
            except OSError:
                continue
            with _cache_lock:
                _file_cache[key] = (st.st_mtime_ns, st.st_size, recs)
        records.extend(recs)
    if provider == "claude":
        seen: set = set()
        unique: List[Record] = []
        for r in sorted(records, key=lambda r: r[0]):
            if r[8] is not None:
                if r[8] in seen:
                    continue
                seen.add(r[8])
            unique.append(r)
        records = unique
    return records


def _hermes_codex_records(cutoff_s: float) -> List[Record]:
    """Hermes' own openai-codex calls (direct to the ChatGPT Codex backend)."""
    out: List[Record] = []
    for profile, path in _profile_dbs():
        try:
            con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=2)
        except sqlite3.Error:
            continue
        try:
            rows = con.execute(
                """
                SELECT u.session_id, u.model, u.input_tokens, u.cache_read_tokens, u.cache_write_tokens,
                       u.output_tokens, u.reasoning_tokens, COALESCE(u.last_seen, u.first_seen, s.started_at)
                FROM session_model_usage u LEFT JOIN sessions s ON s.id = u.session_id
                WHERE u.billing_provider = 'openai-codex'
                  AND COALESCE(u.last_seen, u.first_seen, s.started_at) >= ?
                """,
                (cutoff_s,),
            ).fetchall()
        except sqlite3.Error:
            rows = []
        finally:
            con.close()
        for sid, model, inp, cr, cw, outp, reas, at in rows:
            if not model or not at:
                continue
            outp = _int(outp)
            out.append((int(float(at) * 1000), str(model), _int(inp), _int(cr), _int(cw), outp,
                        min(outp, _int(reas)), "standard", None, f"hermes:{profile}:{sid}"))
    return out


# ─── native accounts (resolved per owning profile, never at import time) ───
def _claude_accounts() -> list[dict]:
    opts = load_feature_module("claude_cli", "options.py")
    accounts, seen = [], set()
    for account in opts.account_definitions():
        directory = opts.config_directory(account["settings"])
        if directory in seen:
            continue
        seen.add(directory)
        accounts.append({**account, "directory": str(directory), "root": directory / "projects"})
    return accounts


def _all_claude_roots() -> List[Path]:
    return [account["root"] for account in _claude_accounts()]


def _claude_records_by_account(cutoff: float) -> list[tuple[dict, List[Record]]]:
    # A copied/forked transcript retains native request IDs: attribute each request only once.
    seen, out = set(), []
    for account in _claude_accounts():
        records = []
        for record in _scan("claude", cutoff, [account["root"]]):
            key = record[8]
            if key is not None:
                if key in seen:
                    continue
                seen.add(key)
            records.append(record)
        out.append((account, records))
    return out


# ─── pricing (Hermes' tables) ───────────────────────────────────────────────
_UNPRICEABLE = {"<synthetic>", "synthetic", "opus", "sonnet", "haiku", "fable"}
_price_cache: Dict[Tuple[str, str], Optional[Tuple[float, float, float, float]]] = {}


def _rates(provider: str, model: str) -> Optional[Tuple[float, float, float, float]]:
    """USD per token for (uncached input, cache read, cache write, output)."""
    base = model.split("[", 1)[0].strip()
    key = (provider, base)
    if key in _price_cache:
        return _price_cache[key]
    rates = None
    native_unpriced = provider in ("antigravity", "muse") and not _native.billing_provider(provider, base)
    if base.lower() not in _UNPRICEABLE and not native_unpriced:
        try:
            from agent.insights import _estimate_cost  # type: ignore
            billing = _native.billing_provider(provider, base) or ("anthropic" if provider == "claude" else "openai")
            m = 1_000_000

            def one(**kw: int) -> Tuple[float, str]:
                return _estimate_cost(base, kw.get("i", 0), kw.get("o", 0),
                                      cache_read_tokens=kw.get("cr", 0), cache_write_tokens=kw.get("cw", 0),
                                      provider=billing)

            ci, si = one(i=m)
            co, so = one(o=m)
            if si != "unknown" and so != "unknown" and (ci > 0 or co > 0):
                cr, _ = one(cr=m)
                cw, _ = one(cw=m)
                rates = (ci / m, (cr or ci) / m, (cw or ci) / m, co / m)
        except Exception:
            rates = None
    _price_cache[key] = rates
    return rates


# Claude fast mode / Codex priority bill at a premium; T3 reads LiteLLM's
# multiple. Hermes has no per-tier table for these, so use the published 2x.
_SPEED_MULTIPLIER = {"standard": 1.0, "fast": 2.0, "ultrafast": 4.0}


# ─── aggregation ────────────────────────────────────────────────────────────
def _bucket_key(ts_ms: int, hourly: bool) -> str:
    d = datetime.fromtimestamp(ts_ms / 1000)
    return d.strftime("%Y-%m-%dT%H:00") if hourly else d.strftime("%Y-%m-%d")


def _empty_tokens() -> Dict[str, int]:
    return {"uncached": 0, "cached": 0, "cacheWrite": 0, "output": 0, "reasoning": 0}


def _account_breakdown(acc_recs: List[Record], cutoff_ms: int) -> Dict[str, Any]:
    """Aggregate tokens+cost for one account's records."""
    tok, cost = 0, 0.0
    empty = _empty_tokens()
    for r in acc_recs:
        ts, model, unc, cac, cw, outp, reas, speed, _, _sid = r
        if ts < cutoff_ms:
            continue
        t = unc + cac + cw + outp
        rates = _rates("claude", model)
        mult = _SPEED_MULTIPLIER.get(speed, 1.0)
        c = sum((unc * rates[0], cac * rates[1], cw * rates[2], outp * rates[3])) * mult if rates else 0.0
        tok += t
        cost += c
        empty["uncached"] += unc
        empty["cached"] += cac
        empty["cacheWrite"] += cw
        empty["output"] += outp
    return {"tokens": tok, "cost": cost, **empty}


def _compute(days: int) -> Dict[str, Any]:
    now = time.time()
    hourly = days == 1
    cutoff = now - (24 * 3600 if hourly else days * 86400)
    if not hourly:
        start = datetime.fromtimestamp(cutoff).replace(hour=0, minute=0, second=0, microsecond=0)
        cutoff = start.timestamp()
    cutoff_ms = int(cutoff * 1000)

    claude_accounts = _claude_records_by_account(cutoff)

    sources = {
        "claude": [("cli", r) for _, records in claude_accounts for r in records],
        "codex": [("cli", r) for r in _scan("codex", cutoff)] + [("hermes", r) for r in _hermes_codex_records(cutoff)],
        "antigravity": [("cli", r) for r in _native.antigravity_records(cutoff)],
        "muse": [("cli", r) for r in _native.muse_records(cutoff)],
    }

    providers: Dict[str, Any] = {}
    models: Dict[Tuple[str, str], Any] = {}
    buckets: Dict[str, Dict[str, Dict[str, float]]] = defaultdict(lambda: defaultdict(lambda: {"tokens": 0, "cost": 0.0}))
    totals = {**_empty_tokens(), "tokens": 0, "cost": 0.0, "unpricedTokens": 0, "cacheSavings": 0.0,
              "categoryCost": {"uncached": 0.0, "cached": 0.0, "cacheWrite": 0.0, "output": 0.0},
              "speedCost": {"standard": 0.0, "fast": 0.0, "ultrafast": 0.0}, "speedPremium": 0.0}

    for provider, recs in sources.items():
        p = {"provider": provider, "tokens": 0, "cost": 0.0, "sessions": set(), "via": defaultdict(int), **_empty_tokens()}
        for origin, r in recs:
            ts, model, unc, cac, cw, outp, reas, speed, _dk, sid = r
            if ts < cutoff_ms:
                continue
            tok = unc + cac + cw + outp
            rates = _rates(provider, model)
            mult = _SPEED_MULTIPLIER.get(speed, 1.0)
            if rates:
                parts = (unc * rates[0], cac * rates[1], cw * rates[2], outp * rates[3])
                cost = sum(parts) * mult
                for k, v in zip(("uncached", "cached", "cacheWrite", "output"), parts):
                    totals["categoryCost"][k] += v * mult
                totals["cacheSavings"] += cac * max(0.0, rates[0] - rates[1]) * mult
                totals["speedPremium"] += sum(parts) * (mult - 1)
            else:
                cost = 0.0
                totals["unpricedTokens"] += tok
            totals["speedCost"][speed if speed in totals["speedCost"] else "standard"] += cost
            for k, v in (("uncached", unc), ("cached", cac), ("cacheWrite", cw), ("output", outp), ("reasoning", reas)):
                p[k] += v
                totals[k] += v
            p["tokens"] += tok
            p["cost"] += cost
            p["via"][origin] += tok
            if sid:
                p["sessions"].add(sid)
            m = models.setdefault((provider, model.split("[", 1)[0]), {
                "provider": provider, "model": model.split("[", 1)[0], "tokens": 0, "cost": 0.0,
                "priced": bool(rates), "sessions": set(), **_empty_tokens()})
            m["tokens"] += tok
            m["cost"] += cost
            m["uncached"] += unc
            m["cached"] += cac
            m["cacheWrite"] += cw
            m["output"] += outp
            if sid:
                m["sessions"].add(sid)
            b = buckets[_bucket_key(ts, hourly)][provider]
            b["tokens"] += tok
            b["cost"] += cost
        totals["tokens"] += p["tokens"]
        totals["cost"] += p["cost"]
        providers[provider] = {**p, "sessions": len(p["sessions"]), "via": dict(p["via"])}

    providers["claude"]["accounts"] = [
        {"id": account["id"], "label": account["label"], **_account_breakdown(records, cutoff_ms)}
        for account, records in claude_accounts
    ]

    # Continuous buckets, oldest → newest, so the chart has a slot per period.
    series = []
    if hourly:
        start = datetime.fromtimestamp(now).replace(minute=0, second=0, microsecond=0)
        keys = [datetime.fromtimestamp(start.timestamp() - h * 3600).strftime("%Y-%m-%dT%H:00") for h in range(23, -1, -1)]
    else:
        keys = [datetime.fromtimestamp(cutoff + d * 86400 + 3600 * 3).strftime("%Y-%m-%d") for d in range(days)]
        today = datetime.fromtimestamp(now).strftime("%Y-%m-%d")
        if keys and keys[-1] != today:
            keys.append(today)
            keys = keys[-days:]
    for k in keys:
        bp = buckets.get(k, {})
        series.append({
            "period": k,
            "byProvider": {pv: {"tokens": v["tokens"], "cost": round(v["cost"], 6)} for pv, v in bp.items()},
            "tokens": sum(v["tokens"] for v in bp.values()),
            "cost": round(sum(v["cost"] for v in bp.values()), 6),
        })

    model_list = sorted(
        ({**m, "sessions": len(m["sessions"])} for m in models.values()),
        key=lambda m: (-m["cost"], -m["tokens"]))

    all_roots = _all_claude_roots()
    return {
        "days": days,
        "resolution": "hour" if hourly else "day",
        "since": int(cutoff * 1000),
        "until": int(now * 1000),
        "providers": providers,
        "models": model_list,
        "series": series,
        "totals": {**totals, "sessions": sum(p["sessions"] for p in providers.values())},
        "sources": {
            "claude": [str(p) for p in all_roots if p.is_dir()],
            "codex": [str(p) for p in CODEX_ROOTS if p.is_dir()] + [f"hermes:{n}" for n, _ in _profile_dbs()],
            "antigravity": [str(p) for p in _native.antigravity_roots()],
            "muse": [str(p) for p in _native.muse_roots()],
        },
    }


@router.get("/usage")
async def get_usage(days: int = Query(30, ge=1, le=365)) -> Dict[str, Any]:
    return await asyncio.to_thread(_compute, days)


# ─── live limits (the CLIs' own answers) ────────────────────────────────────
def _which(name: str) -> Optional[str]:
    found = shutil.which(name)
    if found:
        return found
    for cand in (HOME / ".local/bin" / name, Path("/opt/homebrew/bin") / name, Path("/usr/local/bin") / name,
                 HOME / ".claude/local" / name, HOME / ".npm-global/bin" / name):
        if cand.is_file() and os.access(cand, os.X_OK):
            return str(cand)
    return None


def _iso_from_epoch(v: Any) -> Optional[str]:
    if isinstance(v, (int, float)) and v > 0:
        return datetime.fromtimestamp(v).astimezone().isoformat()
    return None


def _make_limits_env(config_dir: str) -> dict:
    opts = load_feature_module("claude_cli", "options.py")
    return opts.subscription_env({"config_directory": config_dir})


async def _jsonl_session(argv: List[str], frames: List[dict], want, timeout: float,
                         env: Optional[dict] = None) -> Optional[dict]:
    """Spawn a JSON-lines CLI, write ``frames`` (re-sent by ``want`` on demand), and
    return the first frame ``want`` accepts."""
    proc = await asyncio.create_subprocess_exec(
        *argv, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL, limit=16 * 1024 * 1024, env=env)
    try:
        async def send(obj: dict) -> None:
            proc.stdin.write((json.dumps(obj) + "\n").encode())
            await proc.stdin.drain()

        for f in frames:
            await send(f)

        async def pump() -> Optional[dict]:
            while True:
                line = await proc.stdout.readline()
                if not line:
                    return None
                try:
                    msg = json.loads(line)
                except ValueError:
                    continue
                result = await want(msg, send)
                if result is not None:
                    return result

        return await asyncio.wait_for(pump(), timeout)
    finally:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        try:
            await asyncio.wait_for(proc.wait(), 3)
        except Exception:
            pass


def _window(id_: str, label: str, kind: str, used: Any, resets_at: Optional[str], mins: int) -> Dict[str, Any]:
    pct = max(0.0, min(100.0, float(used))) if isinstance(used, (int, float)) else None
    return {"id": id_, "label": label, "kind": kind, "usedPercent": pct, "resetsAt": resets_at, "windowDurationMins": mins}


async def _claude_limits_for_account(account_id: str, account_label: str, config_dir: str, settings=None) -> Dict[str, Any]:
    """Probe one Claude account for rate-limit windows."""
    base: Dict[str, Any] = {"provider": "claude", "account_id": account_id, "account_label": account_label}
    opts = load_feature_module("claude_cli", "options.py")
    try:
        exe = opts.executable(settings or {})
    except FileNotFoundError:
        return {**base, "status": "unavailable", "message": "Claude Code CLI not found."}

    child_env = opts.subscription_env(settings) if settings is not None else _make_limits_env(config_dir)

    # Quick auth check: unauthenticated accounts return a clean status instead of noisy errors.
    try:
        done = await asyncio.to_thread(
            subprocess.run, [exe, "auth", "status"],
            capture_output=True, text=True, env=child_env, timeout=5, check=False)
        auth_info = json.loads(done.stdout)
        base["account"] = auth_info.get("emailAddress") or auth_info.get("email")
        if not auth_info.get("loggedIn"):
            return {**base, "status": "unauthenticated", "message": "Sign in to view limits."}
    except Exception:
        pass  # proceed to probe — auth check failure is not fatal

    async def want(msg: dict, _send) -> Optional[dict]:
        if msg.get("type") != "control_response":
            return None
        resp = msg.get("response") or {}
        return resp if resp.get("request_id") == "t3-usage" else None

    resp = await _jsonl_session(
        [exe, "-p", "--input-format", "stream-json", "--output-format", "stream-json", "--verbose"],
        [{"type": "control_request", "request_id": "t3-init", "request": {"subtype": "initialize"}},
         {"type": "control_request", "request_id": "t3-usage", "request": {"subtype": "get_usage"}}],
        want, PROBE_TIMEOUT_S, env=child_env)
    if not resp or resp.get("subtype") != "success":
        return {**base, "status": "error",
                "message": (resp or {}).get("error") or "Claude Code did not report usage."}
    body = resp.get("response") or {}
    plan = body.get("subscription_type")
    if not body.get("rate_limits_available") or not isinstance(body.get("rate_limits"), dict):
        return {**base, "status": "unsupported", "plan": plan,
                "message": "This Claude account does not report subscription limits."}
    rl = body["rate_limits"]
    windows = []
    for key, label, kind, mins in (("five_hour", "Session", "session", 300), ("seven_day", "Weekly", "weekly", 10080)):
        w = rl.get(key)
        if isinstance(w, dict) and isinstance(w.get("utilization"), (int, float)):
            windows.append(_window(key, label, kind, w["utilization"], w.get("resets_at"), mins))
    for key, label in (("seven_day_opus", "Weekly · Opus"), ("seven_day_sonnet", "Weekly · Sonnet")):
        w = rl.get(key)
        if isinstance(w, dict) and isinstance(w.get("utilization"), (int, float)):
            windows.append(_window(key, label, "weekly", w["utilization"], w.get("resets_at"), 10080))
    for entry in rl.get("model_scoped") or []:
        if isinstance(entry, dict) and isinstance(entry.get("utilization"), (int, float)) and entry.get("display_name"):
            name = str(entry["display_name"])
            windows.append(_window(f"seven_day_{name.lower()}", f"Weekly · {name}", "weekly",
                                   entry["utilization"], entry.get("resets_at"), 10080))
    extra = rl.get("extra_usage") if isinstance(rl.get("extra_usage"), dict) else None
    return {
        **base, "status": "ok", "plan": plan, "windows": windows,
        "extraUsage": ({"enabled": bool(extra.get("is_enabled")), "usedCredits": extra.get("used_credits"),
                        "monthlyLimit": extra.get("monthly_limit"), "currency": extra.get("currency"),
                        "utilization": extra.get("utilization")} if extra else None),
    }


async def _codex_limits() -> Dict[str, Any]:
    exe = _which("codex")
    if not exe:
        return {"provider": "codex", "status": "unavailable", "message": "Codex CLI not found."}
    state: Dict[str, Any] = {}

    async def want(msg: dict, send) -> Optional[dict]:
        if msg.get("id") == 1:
            await send({"jsonrpc": "2.0", "method": "initialized"})
            await send({"jsonrpc": "2.0", "id": 2, "method": "account/read", "params": {}})
            await send({"jsonrpc": "2.0", "id": 3, "method": "account/rateLimits/read"})
            return None
        if msg.get("id") == 2:
            state["account"] = (msg.get("result") or {}).get("account")
            return {"ok": True, **state["limits"]} if "limits" in state else None
        if msg.get("id") == 3:
            if "error" in msg:
                return {"error": msg["error"]}
            state["limits"] = msg.get("result") or {}
            return {"ok": True, **state["limits"]} if "account" in state else None
        return None

    res = await _jsonl_session(
        [exe, "app-server"],
        [{"jsonrpc": "2.0", "id": 1, "method": "initialize",
          "params": {"clientInfo": {"name": "t3code", "title": "T3 Code Usage", "version": "0.2.0"}}}],
        want, PROBE_TIMEOUT_S)
    if not res or "error" in res:
        err = (res or {}).get("error") or {}
        code = f" (JSON-RPC {err.get('code')})" if err else ""
        return {"provider": "codex", "status": "error", "message": f"Codex could not read usage{code}."}
    snap = (res.get("rateLimitsByLimitId") or {}).get("codex") or res.get("rateLimits") or {}
    plan = snap.get("planType") or ((state.get("account") or {}).get("planType"))
    monthly = plan in ("free", "go")
    windows = []
    for pos, fallback in (("primary", 43200 if monthly else 300), ("secondary", 10080)):
        w = snap.get(pos)
        if not isinstance(w, dict) or not isinstance(w.get("usedPercent"), (int, float)):
            continue
        mins = w.get("windowDurationMins") if isinstance(w.get("windowDurationMins"), int) else fallback
        kind = "monthly" if mins >= 43200 else "weekly" if mins >= 10080 else "session"
        windows.append(_window(pos, kind.capitalize() if kind != "session" else "Session", kind,
                               w["usedPercent"], _iso_from_epoch(w.get("resetsAt")), mins))
    credits = res.get("rateLimitResetCredits") or {}
    avail = [c for c in credits.get("credits") or [] if isinstance(c, dict) and c.get("status") == "available"]
    expiries = [c.get("expiresAt") for c in avail if isinstance(c.get("expiresAt"), (int, float))]
    account = state.get("account") or {}
    return {
        "provider": "codex", "status": "ok", "plan": plan, "windows": windows,
        "account": account.get("email"),
        "limitReached": snap.get("rateLimitReachedType"),
        "resetCredits": {"availableCount": int(credits.get("availableCount") or len(avail)),
                         "nextExpiresAt": _iso_from_epoch(min(expiries)) if expiries else None} if credits else None,
        "credits": snap.get("credits"),
    }


# ─── Z.ai GLM Coding Plan ───────────────────────────────────────────────────
_ZAI_UNIT_MINS = {1: 1, 2: 60 * 24 * 7, 3: 60, 4: 60 * 24, 5: 60 * 24 * 30, 6: 60 * 24 * 7}


def _read_env_files() -> Dict[str, str]:
    found: Dict[str, str] = {}
    root = HOME / ".hermes"
    files = [root / ".env"]
    profiles = root / "profiles"
    if profiles.is_dir():
        files += sorted(p / ".env" for p in profiles.iterdir())
    for f in files:
        try:
            for line in f.read_text(encoding="utf-8", errors="replace").splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                k = k.strip().removeprefix("export ").strip()
                if k in ("GLM_API_KEY", "GLM_BASE_URL", "ZAI_API_KEY", "ZAI_BASE_URL") and k not in found:
                    v = v.strip().strip('"').strip("'")
                    if v:
                        found[k] = v
        except OSError:
            continue
    return found


def _zai_credentials() -> Tuple[Optional[str], str]:
    env = {k: os.environ.get(k, "") for k in ("GLM_API_KEY", "GLM_BASE_URL", "ZAI_API_KEY", "ZAI_BASE_URL")}
    files = _read_env_files()
    key = env["GLM_API_KEY"] or env["ZAI_API_KEY"] or files.get("GLM_API_KEY") or files.get("ZAI_API_KEY")
    base = env["GLM_BASE_URL"] or env["ZAI_BASE_URL"] or files.get("GLM_BASE_URL") or files.get("ZAI_BASE_URL") or ""
    origin = "https://open.bigmodel.cn" if "bigmodel.cn" in base else "https://api.z.ai"
    return key or None, origin


def _zai_fetch(key: str, origin: str) -> dict:
    import urllib.request

    req = urllib.request.Request(
        f"{origin}/api/monitor/usage/quota/limit",
        headers={"Authorization": key, "Accept-Language": "en-US,en", "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=PROBE_TIMEOUT_S) as resp:
        return json.loads(resp.read().decode("utf-8"))


async def _zai_limits() -> Dict[str, Any]:
    key, origin = _zai_credentials()
    if not key:
        return {"provider": "zai", "status": "unavailable", "message": "No Z.ai key found (GLM_API_KEY / ZAI_API_KEY)."}
    try:
        body = await asyncio.to_thread(_zai_fetch, key, origin)
    except Exception as exc:
        status = getattr(exc, "code", None)
        return {"provider": "zai", "status": "error",
                "message": "Z.ai rejected the key (is it a Coding Plan key?)." if status in (401, 403)
                else f"Z.ai did not report usage ({str(exc)[:120]})."}
    if not body.get("success") or not isinstance(body.get("data"), dict):
        return {"provider": "zai", "status": "error", "message": body.get("msg") or "Z.ai did not report usage."}
    data = body["data"]
    windows = []
    for i, lim in enumerate(data.get("limits") or []):
        if not isinstance(lim, dict):
            continue
        mins = _ZAI_UNIT_MINS.get(lim.get("unit"), 0) * int(lim.get("number") or 1)
        resets = _iso_from_epoch((lim.get("nextResetTime") or 0) / 1000) if lim.get("nextResetTime") else None
        if lim.get("type") == "TOKENS_LIMIT":
            kind = "weekly" if mins >= 10080 else "monthly" if mins >= 43200 else "session"
            label = {"session": f"Session · {mins // 60}h" if mins and mins % 60 == 0 else "Session",
                     "weekly": "Weekly", "monthly": "Monthly"}[kind]
            w = _window(f"tokens_{i}", label, kind, lim.get("percentage", 0), resets, mins)
            if not resets:
                w["idle"] = True  # window opens with the next request
            windows.append(w)
        elif lim.get("type") == "TIME_LIMIT":
            quota = lim.get("usage")
            used = lim.get("currentValue")
            pct = lim.get("percentage")
            if not isinstance(pct, (int, float)) and isinstance(quota, (int, float)) and quota:
                pct = 100 * (used or 0) / quota
            w = _window(f"mcp_{i}", "MCP tools" + (" · monthly" if mins >= 43200 else ""), "monthly" if mins >= 43200 else "session",
                        pct or 0, resets, mins)
            w["used"], w["limit"], w["unitLabel"] = used, quota, "calls"
            w["details"] = [{"name": d.get("modelCode"), "used": d.get("usage")} for d in lim.get("usageDetails") or []
                            if isinstance(d, dict)]
            windows.append(w)
    level = data.get("level")
    return {"provider": "zai", "status": "ok", "plan": f"GLM Coding {str(level).capitalize()}" if level else "GLM Coding",
            "windows": windows}


_limits_cache: Dict[str, Any] = {"at": 0.0, "data": None, "cache_key": None}
_limits_lock = asyncio.Lock()


async def _safe(coro, provider: str) -> Dict[str, Any]:
    try:
        return await coro
    except asyncio.TimeoutError:
        return {"provider": provider, "status": "error", "message": "The CLI did not answer in time."}
    except Exception as exc:  # pragma: no cover — surfaced to the UI, not raised
        return {"provider": provider, "status": "error", "message": str(exc)[:200]}


@router.get("/limits")
async def get_limits(refresh: bool = False) -> Dict[str, Any]:
    async with _limits_lock:
        now = time.time()
        from hermes_constants import get_hermes_home
        all_accs = _claude_accounts()
        cache_key = (str(get_hermes_home()), tuple((a["id"], a["directory"]) for a in all_accs))
        if (not refresh and _limits_cache["data"]
                and _limits_cache.get("cache_key") == cache_key
                and now - _limits_cache["at"] < LIMITS_TTL_S):
            return _limits_cache["data"]

        async def claude_snapshot(account):
            result = await _safe(_claude_limits_for_account(
                account["id"], account["label"], account["directory"], account["settings"]), "claude")
            return {**result, "account_id": account["id"], "account_label": account["label"]}

        claude_coros = [claude_snapshot(account) for account in all_accs]
        other_coros = [
            _safe(_codex_limits(), "codex"),
            _safe(_zai_limits(), "zai"),
            _safe(asyncio.to_thread(_native.antigravity_limits, _window), "antigravity"),
            _safe(asyncio.to_thread(_native.muse_limits, _window), "muse"),
        ]

        all_results = await asyncio.gather(*claude_coros, *other_coros)
        n = len(all_accs)
        claude_results = list(all_results[:n])
        codex, zai, antigravity, muse = all_results[n], all_results[n + 1], all_results[n + 2], all_results[n + 3]

        # Preserve original provider order: codex, claude(s), zai, antigravity, muse
        providers = [codex] + claude_results + [zai, antigravity, muse]
        data = {"checkedAt": int(now * 1000), "providers": providers}
        _limits_cache.update(at=now, data=data, cache_key=cache_key)
        return data
