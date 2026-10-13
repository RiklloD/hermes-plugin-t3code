"""Portable context handoffs: auditable, bounded transcript digests.

Mirrors T3's ContextHandoff idea: when a target cannot receive context natively (a different
provider, a native runtime, a merge-back delta), the orchestrator materializes an explicit
text artifact, stores it on the transfer record, and injects exactly that text. Nothing is a
hidden prompt hack: the transfer row keeps the full handoff for audit.
"""

from __future__ import annotations

import json
from typing import Any, Iterable, Optional

_HIDDEN_KINDS = {"hidden", "model_switch"}


def _text(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for part in content:
            if isinstance(part, dict):
                if part.get("type") == "text":
                    parts.append(str(part.get("text") or ""))
                elif part.get("type") in ("image_url", "image"):
                    parts.append("[image]")
            elif isinstance(part, str):
                parts.append(part)
        return "\n".join(p for p in parts if p)
    return str(content)


def row_id(msg: dict) -> Optional[int]:
    for key in ("_row_id", "row_id", "id"):
        value = msg.get(key)
        if isinstance(value, int) and not isinstance(value, bool):
            return value
    return None


def visible_user_turns(messages: list[dict]) -> list[int]:
    """Indexes of the user messages that start runs (display-hidden scaffolding excluded)."""
    out = []
    for i, m in enumerate(messages):
        if m.get("role") != "user":
            continue
        if (m.get("display_kind") or "") in _HIDDEN_KINDS:
            continue
        out.append(i)
    return out


def runs(messages: list[dict]) -> list[dict]:
    """Hermes runs: each visible user turn and everything until the next one."""
    starts = visible_user_turns(messages)
    out = []
    for n, start in enumerate(starts):
        end = starts[n + 1] if n + 1 < len(starts) else len(messages)
        chunk = messages[start:end]
        final = next((_text(m.get("content")) for m in reversed(chunk)
                      if m.get("role") == "assistant" and _text(m.get("content")).strip()), "")
        tools = sum(len(m.get("tool_calls") or []) for m in chunk if m.get("role") == "assistant")
        # Completed only when the run ENDS on a final assistant reply (no pending tool calls after it);
        # a tool-call preamble followed by nothing is unfinished work, not a result.
        tail = next((m for m in reversed(chunk) if m.get("role") != "system"), {})
        ended = tail.get("role") == "assistant" and not tail.get("tool_calls") and _text(tail.get("content")).strip()
        status = "completed" if ended else ("interrupted" if len(chunk) > 1 else "no_reply")
        low = final.lower()
        if final and (low.startswith("native runtime failed") or low.startswith("error:") or "task stopped" == low.strip(".")):
            status = "failed" if "fail" in low or "error" in low else "interrupted"
        out.append({
            "ordinal": n + 1,
            "start_index": start,
            "end_index": end,
            "row_id": row_id(messages[start]),
            "user": _text(messages[start].get("content"))[:300],
            "reply": final[:300],
            "tool_calls": tools,
            "status": status,
            "kind": messages[start].get("display_kind") or "user",
        })
    return out


def cut_for_run(messages: list[dict], ordinal: Optional[int]) -> int:
    """Number of leading messages that make up runs 1..ordinal (None = everything)."""
    if ordinal is None:
        return len(messages)
    rs = runs(messages)
    if not rs:
        return len(messages)
    if ordinal < 1 or ordinal > len(rs):
        raise ValueError(f"run {ordinal} does not exist (this thread has {len(rs)} runs)")
    return rs[ordinal - 1]["end_index"]


def _tool_line(call: dict) -> str:
    fn = (call or {}).get("function") or {}
    name = fn.get("name") or call.get("name") or "tool"
    raw = fn.get("arguments")
    args: Any = raw
    if isinstance(raw, str):
        try:
            args = json.loads(raw)
        except ValueError:
            args = raw
    hint = ""
    if isinstance(args, dict):
        for key in ("path", "file_path", "command", "url", "query", "goal", "task", "action"):
            if args.get(key):
                hint = str(args[key])
                break
    elif isinstance(args, str):
        hint = args
    hint = " ".join(hint.split())[:140]
    return f"{name}({hint})" if hint else name


def _items(messages: Iterable[dict], *, tool_results: int = 0) -> list[str]:
    """Readable transcript items. ``tool_results`` > 0 keeps each tool output clipped to that many chars."""
    out = []
    names: dict[str, str] = {}
    for m in messages:
        role = m.get("role")
        kind = m.get("display_kind") or ""
        if kind in _HIDDEN_KINDS:
            continue
        text = _text(m.get("content")).strip()
        if role == "user":
            if text:
                out.append(f"[user]\n{text}")
        elif role == "assistant":
            calls = m.get("tool_calls") or []
            for c in calls:
                if isinstance(c, dict) and c.get("id"):
                    names[str(c["id"])] = ((c.get("function") or {}).get("name") or c.get("name") or "tool")
            if calls:
                out.append("[assistant tool calls] " + "; ".join(_tool_line(c) for c in calls[:12]))
            if text:
                out.append(f"[assistant]\n{text}")
        elif role == "tool":
            # Off by default: tool output is summarized by the calls above; raw output is noise in a handoff.
            if tool_results > 0 and text:
                clip = text if len(text) <= tool_results else text[:tool_results] + "…[clipped]"
                out.append(f"[tool result: {names.get(str(m.get('tool_call_id') or ''), 'tool')}]\n{clip}")
    return out


def _fit(items: list[str], budget: int) -> tuple[list[str], int]:
    """Keep the newest items that fit ``budget`` bytes; long items are clipped. Returns (kept, omitted)."""
    kept: list[str] = []
    used = 0
    for item in reversed(items):
        piece = item if len(item) <= 6000 else item[:3000] + "\n…[clipped]…\n" + item[-2500:]
        size = len(piece.encode("utf-8")) + 2
        if used + size > budget:
            if not kept:
                kept.append(piece.encode("utf-8")[: max(0, budget - 64)].decode("utf-8", "ignore") + "…")
            break
        kept.append(piece)
        used += size
    kept.reverse()
    return kept, len(items) - len(kept)


def portable_context(messages: list[dict], *, title: str, source_key: str, source_provider: str,
                     source_model: str, reason: str, budget: int) -> str:
    """Full-thread handoff for a target that cannot share the source's native state."""
    header = (f"Context handoff ({reason}). Source thread: \"{title}\" ({source_key}), "
              f"previously run on {source_provider or 'unknown provider'}{' / ' + source_model if source_model else ''}.\n"
              "The following is the earlier conversation of this thread, newest last. Reasoning, raw tool output and "
              "attachments are not included; re-check files and state before relying on them.")
    body, omitted = _fit(_items(messages), max(1024, budget - len(header) - 200))
    note = f"\n[{omitted} earlier item(s) omitted to fit the handoff budget]" if omitted else ""
    return f"{header}{note}\n\n" + "\n\n".join(body) + "\n\n[End of context handoff]"


def thread_context(messages: list[dict], *, source: str, target: str, budget: int, delta: bool = False,
                   tool_results: int = 1200) -> str:
    """In-thread harness switch (T3's provider switch inside one thread): the earlier conversation of THIS
    thread, as seen by ``source``, for ``target`` which never saw it natively. ``delta``: only the turns the
    target missed while another harness ran the thread."""
    if delta:
        header = (f"Context handoff (harness switch). While you were away, this same thread continued on {source}. "
                  "Below are the turns you did not see, newest last. Continue from here as the same assistant.")
    else:
        header = (f"Context handoff (harness switch). This thread started on {source} and now continues on {target} "
                  "in the same conversation. Below is the earlier conversation, newest last. Continue from here as "
                  "the same assistant; do not re-introduce yourself.")
    header += (" Reasoning and attachments are not included and tool outputs are clipped; re-check files and state "
               "before relying on them.")
    body, omitted = _fit(_items(messages, tool_results=tool_results), max(1024, budget - len(header) - 200))
    note = f"\n[{omitted} earlier item(s) omitted to fit the handoff budget]" if omitted else ""
    return f"{header}{note}\n\n" + "\n\n".join(body) + "\n\n[End of context handoff]"


def has_content(messages: Iterable[dict]) -> bool:
    return bool(_items(messages))


def delta_context(messages: list[dict], *, fork_title: str, fork_key: str, base_count: int, budget: int) -> str:
    """Merge-back delta: only what happened in the fork after it branched off."""
    delta = messages[base_count:]
    header = (f"Merged context from fork \"{fork_title}\" ({fork_key}). This fork branched from this thread and "
              f"explored separately; below is everything it did after the fork point ({len(delta)} message(s)). "
              "Use its decisions, findings and file changes as context; verify anything it changed on disk.")
    if not delta:
        return header + "\n\n(The fork has no new activity since the fork point.)"
    body, omitted = _fit(_items(delta), max(1024, budget - len(header) - 200))
    note = f"\n[{omitted} earlier fork item(s) omitted to fit the handoff budget]" if omitted else ""
    return f"{header}{note}\n\n" + "\n\n".join(body) + "\n\n[End of merged fork context]"


def last_reply(messages: list[dict]) -> str:
    for m in reversed(messages):
        if m.get("role") == "assistant":
            text = _text(m.get("content")).strip()
            if text:
                return text
    return ""
