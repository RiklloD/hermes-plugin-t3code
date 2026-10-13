"""Translate native SDK messages to host display events; native blocks remain in Claude history."""

from __future__ import annotations

import json


def _content(value) -> str:
    return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)


class Projector:
    def __init__(self, cwd, config_directory):
        self.cwd, self.config_directory = cwd, config_directory
        self.tools = {}
        self.seen_messages = set()

    def project(self, message) -> list[dict]:
        from claude_agent_sdk import AssistantMessage, UserMessage, SystemMessage, ResultMessage
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
        if isinstance(message, SystemMessage):
            if message.subtype == "init":
                return [{"type": "session", "state": {"session_id": message.data["session_id"],
                        "cwd": self.cwd, "config_directory": self.config_directory}}]
            if message.subtype == "compact_boundary":
                return [{"type": "status", "text": "Claude Code compacted its context."}]
            return []
        if isinstance(message, AssistantMessage):
            return self._assistant(message)
        if isinstance(message, UserMessage):
            return self._user(message)
        if isinstance(message, ResultMessage):
            return [self._result(message)]
        return []

    def _assistant(self, message):
        from claude_agent_sdk import TextBlock, ThinkingBlock, ToolUseBlock
        native_id = getattr(message, "uuid", None)
        if native_id and native_id in self.seen_messages:
            return []
        if native_id:
            self.seen_messages.add(native_id)
        # Main history is a display mirror; subagents are presented through their root native tool card.
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

    @staticmethod
    def _result(message):
        reason = getattr(message, "terminal_reason", None)
        data = getattr(message, "data", {}) or {}
        reason = reason or data.get("terminal_reason")
        interrupted = reason in {"aborted_streaming", "aborted_tools"}
        errors = getattr(message, "errors", None) or data.get("errors") or []
        failed = message.is_error or bool(reason and reason not in {"completed", "aborted_streaming", "aborted_tools"})
        return {"type": "result", "text": message.result or "", "usage": message.usage or {},
                "api_calls": message.num_turns,
                "interrupted": interrupted, "error": ("; ".join(map(str, errors)) or reason or message.subtype)
                if failed and not interrupted else None}
