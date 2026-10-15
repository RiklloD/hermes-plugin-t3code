"""Narrow Hermes surface for Claude: delegate tasks to other Hermes profiles through the official CLI.

Claude never receives Hermes credentials or tools. It gets an in-process MCP server whose spawning tools
are pre-approved in `full` mode (default) or go through the normal Hermes approval request in `approve` mode. Each delegated task is
a separate `hermes -p <profile> chat` process whose record lives on disk, so status survives restarts.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import signal
import subprocess
import sys
import time
import uuid
from pathlib import Path

SERVER = "hermes"
READ_TOOLS = ("hermes_profiles", "hermes_task_status", "hermes_task_list")
SPAWN_TOOLS = ("hermes_task_start", "hermes_task_stop")
MODES = {"off", "read_only", "approve", "full"}
_TASK_ID = re.compile(r"^[0-9a-f]{12}$")
_OUTPUT_TAIL = 12000
_MAX_WAIT = 600
# Records the child's exit code so a restarted runtime can still report a finished task.
_WRAPPER = ("import subprocess,sys\n"
            "with open(sys.argv[1],'wb') as out:\n"
            "    code=subprocess.run(sys.argv[3:],stdout=out,stderr=subprocess.STDOUT,stdin=subprocess.DEVNULL).returncode\n"
            "open(sys.argv[2],'w').write(str(code))\n")


def tool_names(names) -> list[str]:
    return [f"mcp__{SERVER}__{name}" for name in names]


def _text(value, error=False) -> dict:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, indent=2)
    return {"content": [{"type": "text", "text": text}], "is_error": error}


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


class TaskRegistry:
    """Delegated Hermes tasks owned by one Claude conversation."""

    def __init__(self, root: Path, hermes: str, environment: dict, cwd: str, owner):
        self.root, self.hermes, self.environment, self.cwd = Path(root), hermes, environment, cwd
        self.owner = owner  # callable: current native session id, set after the first init message
        self.root.mkdir(parents=True, exist_ok=True)
        self._processes = {}  # keeps Popen handles alive so finished children are reaped, not leaked

    def _paths(self, task_id):
        if not _TASK_ID.match(task_id or ""):
            raise ValueError(f"Unknown task id: {task_id}")
        base = self.root / task_id
        return base.with_suffix(".json"), base.with_suffix(".log"), base.with_suffix(".exit")

    def _record(self, task_id) -> dict:
        meta, _, _ = self._paths(task_id)
        if not meta.exists():
            raise ValueError(f"Unknown task id: {task_id}")
        record = json.loads(meta.read_text())
        if record.get("owner") != self.owner():
            raise ValueError(f"Task {task_id} belongs to another conversation")
        return record

    def _profile_exists(self, profile: str) -> bool:
        try:
            from hermes_cli.profiles import profile_exists
        except ImportError:
            done = subprocess.run([self.hermes, "profile", "show", profile], capture_output=True, timeout=30, check=False)
            return done.returncode == 0
        return profile_exists(profile)

    def start(self, profile: str, task: str, *, cwd: str = "", worktree: bool = False) -> dict:
        if not self._profile_exists(profile):
            raise ValueError(f"Hermes profile '{profile}' does not exist. Call hermes_profiles first.")
        if not task.strip():
            raise ValueError("Task text is empty")
        workdir = str(Path(cwd or self.cwd).expanduser().resolve())
        if not Path(workdir).is_dir():
            raise ValueError(f"Working directory does not exist: {workdir}")
        task_id = uuid.uuid4().hex[:12]
        meta, log, exit_file = self._paths(task_id)
        query = meta.with_suffix(".prompt")
        query.write_text(task)
        argv = [self.hermes, "-p", profile, "chat", "--query-file", str(query), "-Q",
                "--source", "claude-cli", "--in", workdir]
        if worktree:
            argv.append("--worktree")
        process = subprocess.Popen([sys.executable, "-I", "-c", _WRAPPER, str(log), str(exit_file), *argv],
                                   cwd=workdir, env=self.environment, start_new_session=True,
                                   stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self._processes[task_id] = process
        record = {"task_id": task_id, "profile": profile, "task": task, "cwd": workdir, "worktree": worktree,
                  "pid": process.pid, "started_at": time.time(), "owner": self.owner()}
        meta.write_text(json.dumps(record))
        return {"task_id": task_id, "profile": profile, "status": "running"}

    def status(self, task_id: str) -> dict:
        record = self._record(task_id)
        _, log, exit_file = self._paths(task_id)
        output = log.read_text(errors="replace") if log.exists() else ""
        if exit_file.exists():
            code = exit_file.read_text().strip()
            state = "completed" if code == "0" else ("stopped" if record.get("stopped") else "failed")
        elif self._running(task_id, record["pid"]):
            code, state = None, "running"
        else:
            code, state = None, "stopped" if record.get("stopped") else "lost"
        return {"task_id": task_id, "profile": record["profile"], "status": state,
                "exit_code": int(code) if code not in (None, "") else None,
                "elapsed_seconds": int(time.time() - record["started_at"]),
                "output": output[-_OUTPUT_TAIL:], "truncated": len(output) > _OUTPUT_TAIL}

    def _running(self, task_id, pid) -> bool:
        # Our own child must be polled (reaped): an unreaped zombie still answers kill(pid, 0).
        process = self._processes.get(task_id)
        return process.poll() is None if process is not None else _alive(pid)

    async def wait(self, task_id: str, seconds: float) -> dict:
        deadline = time.monotonic() + max(0.0, min(float(seconds or 0), _MAX_WAIT))
        while True:
            result = self.status(task_id)
            if result["status"] != "running" or time.monotonic() >= deadline:
                return result
            await asyncio.sleep(1)

    def list(self) -> list[dict]:
        rows = []
        for meta in sorted(self.root.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True):
            try:
                row = self.status(meta.stem)
            except (ValueError, OSError):
                continue
            rows.append({k: row[k] for k in ("task_id", "profile", "status", "elapsed_seconds")})
        return rows

    def stop(self, task_id: str) -> dict:
        record = self._record(task_id)
        meta, _, _ = self._paths(task_id)
        if self.status(task_id)["status"] == "running":
            record["stopped"] = True
            meta.write_text(json.dumps(record))
            try:
                os.killpg(record["pid"], signal.SIGTERM)
            except ProcessLookupError:
                pass
        return self.status(task_id)


def profiles(hermes: str = "hermes"):
    try:
        from hermes_cli.profiles import list_profiles
    except ImportError:
        # Outside a Hermes interpreter: the CLI's own table is still authoritative.
        done = subprocess.run([hermes, "profile", "list"], capture_output=True, text=True, timeout=30, check=False)
        return done.stdout or done.stderr
    return [{"name": p.name, "default": p.is_default, "model": p.model, "provider": p.provider}
            for p in list_profiles(lazy_skill_count=True)]


def build_server(registry: TaskRegistry, mode: str):
    """Return (mcp server config, tool names pre-approved without a Hermes approval)."""
    from claude_agent_sdk import ToolAnnotations, create_sdk_mcp_server, tool

    read_only = ToolAnnotations(readOnlyHint=True)

    def guarded(fn):
        async def call(args):
            try:
                return _text(await fn(args))
            except (ValueError, OSError) as exc:
                return _text(str(exc), error=True)
        return call

    async def _profiles(_args):
        return await asyncio.to_thread(profiles, registry.hermes)

    async def _status(args):
        return await registry.wait(args["task_id"], args.get("wait_seconds") or 0)

    async def _list(_args):
        return registry.list()

    async def _start(args):
        return registry.start(args["profile"], args["task"], cwd=args.get("cwd") or "",
                              worktree=bool(args.get("worktree")))

    async def _stop(args):
        return registry.stop(args["task_id"])

    tools = [
        tool("hermes_profiles", "List the user's Hermes agent profiles (name, model, provider). "
             "Each profile is a separate Hermes agent with its own model, memory, skills and tools.",
             {"type": "object", "properties": {}}, read_only)(guarded(_profiles)),
        tool("hermes_task_status", "Read a delegated Hermes task's status and output. Set wait_seconds "
             f"(max {_MAX_WAIT}) to block until it finishes.",
             {"type": "object", "properties": {"task_id": {"type": "string"},
              "wait_seconds": {"type": "number"}}, "required": ["task_id"]}, read_only)(guarded(_status)),
        tool("hermes_task_list", "List Hermes tasks delegated from this conversation.",
             {"type": "object", "properties": {}}, read_only)(guarded(_list)),
    ]
    if mode in ("approve", "full"):
        tools += [
            tool("hermes_task_start", "Start a task on another Hermes profile (a separate agent). Runs in the "
                 "background and returns a task_id; poll hermes_task_status. Write a self-contained task: the "
                 "other agent cannot see this conversation.",
                 {"type": "object", "properties": {
                     "profile": {"type": "string", "description": "Profile name from hermes_profiles"},
                     "task": {"type": "string", "description": "Complete instructions for the other agent"},
                     "cwd": {"type": "string", "description": "Working directory (default: this workspace)"},
                     "worktree": {"type": "boolean", "description": "Run in an isolated git worktree"}},
                  "required": ["profile", "task"]})(guarded(_start)),
            tool("hermes_task_stop", "Stop a running delegated Hermes task.",
                 {"type": "object", "properties": {"task_id": {"type": "string"}}, "required": ["task_id"]},
                 )(guarded(_stop)),
        ]
    server = create_sdk_mcp_server(SERVER, version="1.0.0", tools=tools)
    return server, tool_names(READ_TOOLS + SPAWN_TOOLS if mode == "full" else READ_TOOLS)
