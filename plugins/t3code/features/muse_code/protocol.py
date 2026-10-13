"""Muse Session Protocol (MSP) mapping, ported from T3 Code's ``provider-muse``
(``protocol.ts``, ``itemPresentation.ts``, ``sdk.ts``, ``modelCatalog.ts``)."""

from __future__ import annotations

import json
import os
import re
from typing import Optional

# Muse's own rows for these tools carry the work; the tool call is housekeeping (adapter.ts).
TOOLS_WITH_NATIVE_ROWS = {"write_todos", "request_user_input", "workflow"}
# Muse's skill-reminder child runs on every model step; it is housekeeping, not work.
HIDDEN_KINDS = {"userMessage", "reminderChild"}
REASONING_EFFORTS = ["none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"]
FALLBACK_EFFORTS = ["low", "medium", "high", "xhigh", "max"]
FALLBACK_DEFAULT_EFFORT = "high"


def muse_environment(environment: dict) -> dict:
    """Preserve the host's CLI login while excluding the API-key override (``makeMuseEnvironment``)."""
    env = {k: v for k, v in environment.items() if k.upper() != "META_API_KEY"}
    env["MUSE_NO_AUTO_UPDATE"] = "1"
    return env


def approval_mode(runtime_mode: str) -> str:
    return "allowAll" if runtime_mode == "full-access" else "promptUnmatched"


def serve_args(*, read_only=False, session_logging=False, runtime_mode="approval-required") -> list[str]:
    """``museServeArgs``: read-only hosts get no shell, writes, or workspace trust."""
    args = ["serve"]
    if read_only:
        args += ["--disable-shell", "--disable-write"]
        if not session_logging:
            args.append("--no-session-log")
    else:
        args.append("--trust-workspace")
        if runtime_mode == "full-access":
            args.append("--disable-sandbox")
    return args


def initialize_params(read_only=False) -> dict:
    return {"clientInfo": {"name": "hermes_t3code", "title": "Hermes · T3 Code", "version": "1"},
            "capabilities": {"requestedCapabilities": [] if read_only else ["sessionMcp"]}}


def workspace_root(path: str) -> str:
    """``turn/start`` only accepts the canonical path (macOS: ``/tmp`` → ``/private/tmp``)."""
    return os.path.realpath(path)


def parse_version(output: str) -> Optional[str]:
    match = re.search(r"\b\d+\.\d+\.\d+-R\d+(?:\.\d+)?\b", output) or re.search(r"\b\d+\.\d+\.\d+\b", output)
    return match[0] if match else None


# ── approvals (protocol.ts) ──────────────────────────────────────────────────────────────────────

def approval_decision(choice: dict) -> Optional[str]:
    decision, scope = choice.get("decision"), choice.get("scope")
    if decision == "approved":
        return "accept"
    if decision == "approvedForSession":
        return "acceptForSession"
    if decision == "approvedPolicyAmendment":
        return {"session": "acceptForSession", "localPersistent": "acceptAlways"}.get(scope)
    if decision in ("denied", "deniedPolicyAmendment"):
        return "decline"
    if decision == "abort":
        return "cancel"
    return None


def approval_choices(approval: dict) -> dict[str, dict]:
    """One native choice per decision. Shell approvals offer no ``denied``, only ``abort``."""
    def rank(choice):
        scope = choice.get("scope")
        return (0 if scope == "once" else 2 if scope == "session" else 4) + \
            (1 if str(choice.get("decision", "")).endswith("PolicyAmendment") else 0)
    choices: dict[str, dict] = {}
    for choice in approval.get("availableChoices") or []:
        decision = approval_decision(choice)
        if decision and (decision not in choices or rank(choice) < rank(choices[decision])):
            choices[decision] = choice
    if "decline" not in choices and "cancel" in choices:
        choices["decline"] = choices["cancel"]
    return choices


def approval_prompt(approval: dict) -> str:
    subject = approval.get("subject") or {}
    return (subject.get("command") or subject.get("path") or subject.get("host") or subject.get("target")
            or approval.get("toolName") or "Muse requests permission")


def question_answers(request: dict, answers: dict) -> list[dict]:
    """``userInput/answer`` rows from Hermes answers (``qid`` → label(s) or free text)."""
    rows = []
    for question in request.get("questions") or []:
        raw = answers.get(question["id"])
        values = [raw] if isinstance(raw, str) else list(dict.fromkeys(raw or []))
        labels = {o.get("label") for o in question.get("options") or []}
        selected = [v for v in values if v in labels]
        free = "\n".join(v for v in values if v not in selected)
        if len(free) > 500 or (not free and not selected):
            raise ValueError("Muse needs a nonempty answer with at most 500 custom characters")
        row = {"questionId": question["id"]}
        if selected:
            single = (question.get("selection") or {}).get("mode") != "multiple"
            row.update({"selectedLabel": selected[0]} if single else {"selectedLabels": selected})
        if free:
            row["note" if selected else "freeText"] = free
        rows.append(row)
    return rows


# ── items (itemPresentation.ts) ──────────────────────────────────────────────────────────────────

def item_status(item: dict) -> str:
    status = item.get("status")
    if status == "inProgress":
        return "running"
    if status == "cancelled":
        return "cancelled"
    if status == "completed" and not (item.get("kind") == "compaction" and item.get("outcome")
                                      and item["outcome"] != "compacted"):
        return "completed"
    return "failed"


def tool_args(item: dict) -> dict:
    try:
        args = json.loads(item.get("args") or "")
    except ValueError:  # streaming model-authored arguments may not be valid JSON yet
        return {}
    return args if isinstance(args, dict) else {}


def tool_name(item: dict) -> str:
    kind = item.get("kind")
    if kind == "subagent":
        return "subagent"
    if kind == "userShell" or re.search(r"shell|bash|exec_command", item.get("tool") or "", re.I):
        return item.get("tool") or "shell"
    return item.get("tool") or kind or "tool"


def tool_input(item: dict) -> dict:
    args = tool_args(item)
    if item.get("kind") == "userShell" and item.get("commandText"):
        return {"command": item["commandText"]}
    if item.get("kind") == "subagent":
        return {k: item[k] for k in ("objective", "role") if item.get(k)}
    if item.get("kind") == "workflow":
        return {"entry": item.get("entryId") or "Workflow"}
    return args or ({"args": item["args"]} if item.get("args") else {})


def tool_output(item: dict) -> str:
    status = item_status(item)
    detail = (item.get("failureReason") or f"Muse reported {item.get('status')}.") if status == "failed" else None
    summary = item.get("summary")
    text = (item.get("visibleOutput") or (item.get("result") or {}).get("summary") or item.get("text")
            or ("\n".join(summary) if summary else None) or item.get("finalSummary")
            or item.get("fallbackText") or item.get("objective") or "")
    exit_code = item.get("exitCode")
    try:  # Muse's bash tool returns a JSON envelope; show its output like a terminal
        envelope = json.loads(text) if text.startswith("{") else None
    except ValueError:
        envelope = None
    if isinstance(envelope, dict) and isinstance(envelope.get("output"), str):
        text, exit_code = envelope["output"].rstrip(), envelope.get("exit_code", exit_code)
    if exit_code not in (None, 0) and not detail:
        text = f"{text}\n[exit code {exit_code}]".strip()
    return "\n".join(filter(None, [detail, text])) if detail else text


def is_tool(item: dict) -> bool:
    kind = item.get("kind")
    if kind in HIDDEN_KINDS or kind in ("agentMessage", "reasoning", "compaction"):
        return False
    return not (kind == "toolCall" and item.get("tool") in TOOLS_WITH_NATIVE_ROWS and item.get("status") != "failed")


def model_rows(catalog: dict) -> list[dict]:
    """``discoverMuseModels``: Meta's catalog only, first entry per model id."""
    if catalog.get("providerId") != "meta":
        raise ValueError("Muse returned a catalog for another provider.")
    seen, rows = set(), []
    for model in catalog.get("models") or []:
        model_id = model.get("modelId")
        if model.get("providerId") != "meta" or not model_id or model_id in seen:
            continue
        seen.add(model_id)
        variants = model.get("variants")
        efforts = [e for e in (variants if isinstance(variants, list) else FALLBACK_EFFORTS) if e in REASONING_EFFORTS]
        rows.append({"id": model_id, "label": (model.get("displayLabel") or "").strip() or model_id,
                     "is_default": bool(model.get("isDefault")),
                     "context_window": int(model.get("contextLimit") or 0) or 1_000_000,
                     "efforts": efforts,
                     "default_effort": model.get("defaultReasoningEffort") if isinstance(variants, list)
                     else FALLBACK_DEFAULT_EFFORT})
    return rows


def resolve_effort(row: Optional[dict], effort: str) -> Optional[str]:
    """``resolveMuseReasoningEffort``: an unsupported choice falls back to the model's default."""
    efforts = (row or {}).get("efforts") or FALLBACK_EFFORTS
    if effort in efforts:
        return effort
    default = (row or {}).get("default_effort") or FALLBACK_DEFAULT_EFFORT
    return default if default in efforts else (efforts[0] if efforts else None)
