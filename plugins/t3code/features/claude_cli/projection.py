"""Translate native SDK messages to host display events; native blocks remain in Claude history."""

from __future__ import annotations

import json
import time
from datetime import datetime

TERMINAL_TASK_STATUSES = {"completed", "failed", "stopped", "killed"}
_PROGRESS_INTERVAL = 15.0
_SUMMARY_LIMIT = 600
_TASK_KINDS = {"local_agent": "Subagent", "local_bash": "Background command", "local_workflow": "Workflow"}


def _content(value) -> str:
    return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)


def _clip(text, limit=_SUMMARY_LIMIT) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[:limit - 1] + "…"


def _clock(epoch) -> str:
    try:
        return datetime.fromtimestamp(int(epoch)).strftime("%a %H:%M")
    except (TypeError, ValueError, OSError):
        return "unknown time"


# Claude Code's /context categories as Hermes context-meter rows. Only ``kind: used`` occupies the
# window: deferred tools, the autocompact buffer and free space do not.
_CONTEXT_IDS = {"system prompt": "system_prompt", "system tools": "tool_definitions", "mcp tools": "mcp",
                "custom agents": "subagent_definitions", "memory files": "rules", "skills": "skills",
                "messages": "conversation"}


def context_snapshot(raw) -> dict | None:
    """``get_context_usage()`` as the host's ``context_usage()`` reading; None when it reports no window."""
    maximum = int((raw or {}).get("rawMaxTokens") or (raw or {}).get("maxTokens") or 0)
    if not maximum:
        return None
    rows = {}
    for cat in raw.get("categories") or []:
        name = str(cat.get("name") or "").strip()
        if not name or cat.get("isDeferred") or cat.get("kind", "used") != "used" or not cat.get("tokens"):
            continue
        cid = _CONTEXT_IDS.get(name.lower(), name.lower().replace(" ", "_"))
        rows.setdefault(cid, {"id": cid, "label": name, "tokens": 0})["tokens"] += int(cat["tokens"])
    return {"context_used": int(raw.get("totalTokens") or 0), "context_max": maximum,
            "model": raw.get("model") or "", "categories": list(rows.values())}


def injected_origin(message) -> str | None:
    """Origin kind of a turn the CLI started on its own (task notification, peer, ...); None for ours."""
    origin = getattr(message, "origin", None)
    kind = origin.get("kind") if isinstance(origin, dict) else None
    return kind if kind and kind != "human" else None


class TaskRoster:
    """Background shells and subagents survive the turn that started them; one roster per session."""

    def __init__(self):
        self.tasks = {}
        self._progress_at = {}

    @property
    def active(self) -> list[dict]:
        return [task for task in self.tasks.values() if task["status"] not in TERMINAL_TASK_STATUSES]

    def project(self, message) -> list[dict]:
        from claude_agent_sdk import (TaskNotificationMessage, TaskProgressMessage, TaskStartedMessage,
                                      TaskUpdatedMessage)
        if isinstance(message, TaskStartedMessage):
            kind = _TASK_KINDS.get(message.task_type or "local_agent", "Background task")
            self.tasks[message.task_id] = {"id": message.task_id, "description": message.description,
                                           "kind": kind, "status": "running", "reported": False}
            return [{"type": "status", "text": f"{kind} started: {_clip(message.description, 160)}"}]
        task = self.tasks.get(getattr(message, "task_id", None))
        if isinstance(message, TaskProgressMessage):
            if task is None:
                return []
            now = time.monotonic()
            if now - self._progress_at.get(message.task_id, 0.0) < _PROGRESS_INTERVAL:
                return []
            self._progress_at[message.task_id] = now
            usage = message.usage or {}
            last = f", last: {message.last_tool_name}" if message.last_tool_name else ""
            return [{"type": "status", "text": f"{task['kind']} '{_clip(task['description'], 80)}': "
                     f"{usage.get('tool_uses', 0)} tool calls{last}"}]
        if isinstance(message, TaskNotificationMessage):
            return self._finish(message.task_id, message.status, message.summary)
        if isinstance(message, TaskUpdatedMessage):
            status = message.status or (message.patch or {}).get("status")
            if status in TERMINAL_TASK_STATUSES:
                return self._finish(message.task_id, status, "")
        return []

    def _finish(self, task_id, status, summary) -> list[dict]:
        task = self.tasks.setdefault(task_id, {"id": task_id, "description": task_id, "kind": "Background task",
                                              "status": status, "reported": False})
        task["status"] = status
        self._progress_at.pop(task_id, None)
        if task["reported"]:
            return []
        task["reported"] = True
        label = {"completed": "finished", "failed": "failed"}.get(status, "stopped")
        detail = f": {_clip(summary)}" if summary else ""
        return [{"type": "status", "text": f"{task['kind']} {label} — {_clip(task['description'], 120)}{detail}"}]


class Projector:
    def __init__(self, cwd, config_directory, roster: TaskRoster | None = None):
        self.cwd, self.config_directory = cwd, config_directory
        self.roster = roster or TaskRoster()
        self.tools = {}
        self.seen_messages = set()
        self.rate_limit = None

    def project(self, message) -> list[dict]:
        from claude_agent_sdk import AssistantMessage, RateLimitEvent, ResultMessage, SystemMessage, UserMessage
        from claude_agent_sdk.types import StreamEvent
        if isinstance(message, StreamEvent):
            delta = (message.event or {}).get("delta") or {}
            # Subagent text must not be concatenated into the main assistant's answer.
            if message.parent_tool_use_id:
                return []
            if delta.get("type") == "text_delta":
                return [{"type": "text_delta", "text": delta.get("text", "")}]
            if delta.get("type") == "thinking_delta":
                return [{"type": "reasoning_delta", "text": delta.get("thinking", "")}]
            return []
        if isinstance(message, RateLimitEvent):
            return self._rate_limit(message.rate_limit_info)
        if isinstance(message, SystemMessage):
            if message.subtype == "init":
                return [{"type": "session", "state": {"session_id": message.data["session_id"],
                        "cwd": self.cwd, "config_directory": self.config_directory}}]
            if message.subtype == "compact_boundary":
                meta = (message.data or {}).get("compact_metadata") or {}
                detail = []
                if meta.get("trigger"):
                    detail.append(meta["trigger"])
                if meta.get("pre_tokens"):
                    detail.append(f"{int(meta['pre_tokens']):,} tokens before")
                suffix = f" ({', '.join(detail)})" if detail else ""
                return [{"type": "status", "text": f"Claude Code compacted its context{suffix}."}]
            return self.roster.project(message)
        if isinstance(message, AssistantMessage):
            return self._assistant(message)
        if isinstance(message, UserMessage):
            return self._user(message)
        if isinstance(message, ResultMessage):
            return [self._result(message)]
        return []

    def _rate_limit(self, info) -> list[dict]:
        self.rate_limit = info
        window = (info.rate_limit_type or "usage").replace("_", " ")
        resets = f"; resets {_clock(info.resets_at)}" if info.resets_at else ""
        if info.status == "rejected":
            return [{"type": "status", "text": f"Claude {window} limit reached{resets}."}]
        if info.status == "allowed_warning":
            used = f" {round(info.utilization * 100)}%" if info.utilization is not None else ""
            return [{"type": "status", "text": f"Claude {window} limit warning:{used} used{resets}."}]
        return []

    def _assistant(self, message):
        from claude_agent_sdk import TextBlock, ThinkingBlock, ToolUseBlock
        native_id = getattr(message, "uuid", None)
        if native_id and native_id in self.seen_messages:
            return []
        if native_id:
            self.seen_messages.add(native_id)
        # Main history is a display mirror; subagents are presented through their root tool card and task status.
        if getattr(message, "parent_tool_use_id", None):
            return []
        texts, thoughts, calls, events = [], [], [], []
        for block in message.content:
            if isinstance(block, TextBlock):
                texts.append(block.text)
            elif isinstance(block, ThinkingBlock):
                thoughts.append(block.thinking)
            elif isinstance(block, ToolUseBlock):
                self.tools[block.id] = (block.name, block.input)
                calls.append({"id": block.id, "type": "function", "function": {
                    "name": block.name, "arguments": json.dumps(block.input, ensure_ascii=False)}})
                events.append({"type": "tool_start", "id": block.id, "name": block.name, "input": block.input})
        if not texts and not thoughts and not calls:
            return []
        return [{"type": "assistant", "text": "\n".join(texts), "reasoning": "\n".join(thoughts),
                 "tool_calls": calls}, *events]

    def _user(self, message):
        from claude_agent_sdk import ToolResultBlock
        if getattr(message, "parent_tool_use_id", None) or not isinstance(message.content, list):
            return []
        events = []
        for block in message.content:
            if isinstance(block, ToolResultBlock):
                name, inputs = self.tools.pop(block.tool_use_id, ("native_tool", {}))
                events.append({"type": "tool_result", "id": block.tool_use_id, "name": name,
                               "input": inputs, "content": _content(block.content), "is_error": block.is_error})
        return events

    def _result(self, message):
        reason = getattr(message, "terminal_reason", None)
        data = getattr(message, "data", {}) or {}
        reason = reason or data.get("terminal_reason")
        interrupted = reason in {"aborted_streaming", "aborted_tools"}
        errors = getattr(message, "errors", None) or data.get("errors") or []
        failed = message.is_error or bool(reason and reason not in {"completed", "aborted_streaming", "aborted_tools"})
        error = ("; ".join(map(str, errors)) or reason or message.subtype) if failed and not interrupted else None
        limited = bool(error) and (getattr(message, "api_error_status", None) == 429 or
                                   (self.rate_limit is not None and self.rate_limit.status == "rejected"))
        if limited and self.rate_limit is not None and self.rate_limit.resets_at:
            error = f"{error} (Claude usage limit; resets {_clock(self.rate_limit.resets_at)})"
        return {"type": "result", "text": message.result or "", "usage": message.usage or {},
                "api_calls": message.num_turns, "interrupted": interrupted, "error": error,
                "rate_limited": limited,
                "resets_at": self.rate_limit.resets_at if limited and self.rate_limit is not None else None}
