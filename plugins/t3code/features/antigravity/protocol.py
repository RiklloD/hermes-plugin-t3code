"""Antigravity's ACP dialect, ported from T3 Code (``AntigravityProtocol.ts``,
``AntigravityAcpSupport.ts``, ``antigravityAuthSupport.ts``)."""

from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path
from typing import Optional

AUTH_METHODS = ("oauth-personal", "oauth-business", "gemini-api-key", "agent-platform")
AUTH_URL_PREFIX = "Open the following link to authenticate the ACP server: "
SIGN_IN_REQUIRED = "Sign in to Antigravity before you continue (antigravity_login)."
TOOL_TEXT_LIMIT = 8000

# Only the configured method's credential reaches the agent; ambient Google variables never do.
REMOVED_ENV = {
    "GEMINI_API_KEY", "GOOGLE_API_KEY", "GOOGLE_APPLICATION_CREDENTIALS", "GOOGLE_CLOUD_PROJECT",
    "GOOGLE_CLOUD_LOCATION", "GOOGLE_CLOUD_QUOTA_PROJECT", "GOOGLE_GENAI_USE_VERTEXAI", "GCLOUD_PROJECT",
    "CLOUDSDK_CORE_PROJECT", "AGY_ACP_CCPA_PROJECT", "AGY_ACP_ENABLE_OAUTH", "GEMINI_HOME",
    "AGY_ACP_FORCE_FILE_STORAGE", "ANTIGRAVITY_HARNESS_PATH", "BROWSER", "PYTHONUNBUFFERED",
    "ELECTRON_RUN_AS_NODE",
}


def auth_config(settings: dict) -> dict:
    method = str(settings.get("auth_method") or "oauth-personal")
    if method not in AUTH_METHODS:
        raise ValueError(f"antigravity_auth_method must be one of {', '.join(AUTH_METHODS)}")
    return {"method": method, "api_key": str(settings.get("api_key") or "").strip(),
            "gcp_project": str(settings.get("gcp_project") or "").strip(),
            "gcp_location": str(settings.get("gcp_location") or "").strip()}


def uses_browser(method: str) -> bool:
    return method in ("oauth-personal", "oauth-business")


def environment(base: dict, profile: Path, runtime: dict, auth: dict) -> dict:
    """``buildAntigravityAcpSpawnInput``: a private GEMINI_HOME, file token storage, own temp dir."""
    # Windows treats environment keys as case-insensitive: drop aliases too.
    env = {k: v for k, v in base.items() if k.upper() not in REMOVED_ENV}
    if auth["method"] == "gemini-api-key" and auth["api_key"]:
        env["GEMINI_API_KEY"] = auth["api_key"]
    elif auth["method"] == "agent-platform" and auth["api_key"]:
        env["GOOGLE_API_KEY"] = auth["api_key"]
    # The agent is a PyInstaller one-file bundle that unpacks ~1 GB per launch; keep it reclaimable.
    temp = profile / "antigravity-acp" / "tmp"
    temp.mkdir(parents=True, exist_ok=True)
    env.update(GEMINI_HOME=str(profile), AGY_ACP_FORCE_FILE_STORAGE="1", PYTHONUNBUFFERED="1",
               ELECTRON_RUN_AS_NODE="1", ANTIGRAVITY_HARNESS_PATH=runtime["harness"],
               **({"TEMP": str(temp), "TMP": str(temp)} if os.name == "nt" else {"TMPDIR": str(temp)}))
    if os.name != "nt":
        # Never let the agent open a browser on its own (a background probe must stay silent);
        # interactive sign-in reads the printed link and opens it itself.
        env["BROWSER"] = shutil.which("true") or "/usr/bin/true"
    return env


def argv(runtime: dict) -> list[str]:
    return [runtime["executable"], *(["--uid="] if sys.platform.startswith("linux") else [])]


def prepare_profile(profile: Path, auth: dict) -> None:
    """Write ``antigravity-acp/settings.json`` (never a credential) and link the user's global skills.

    The agent reads user-global skills under GEMINI_HOME, which points at the private profile; link
    the two skill directories back to the real ``~/.gemini`` (``linkAntigravityUserSkills``).
    """
    acp = profile / "antigravity-acp"
    acp.mkdir(parents=True, exist_ok=True)
    gcp = {k: v for k, v in (("project", auth["gcp_project"]), ("location", auth["gcp_location"])) if v}
    text = json.dumps({"auth": {"type": auth["method"]}, **({"gcp": gcp} if gcp else {})}, separators=(",", ":")) + "\n"
    settings = acp / "settings.json"
    if not settings.is_file() or settings.read_text() != text:
        settings.write_text(text)
    user = Path.home() / ".gemini"
    for rel in (("config", "skills"), ("antigravity-cli", "skills")):
        link, target = profile.joinpath(*rel), user.joinpath(*rel)
        try:
            if link.is_symlink() and Path(os.readlink(link)) == target:
                continue
            if link.is_symlink():
                link.unlink()
            elif link.exists():
                continue  # a real directory is the user's own content
            link.parent.mkdir(parents=True, exist_ok=True)
            os.symlink(target, link, target_is_directory=True)
        except OSError:
            pass  # a missing link only costs global skills, never the session


def permission_mode(runtime_mode: str) -> str:
    return {"full-access": "yolo", "auto-accept-edits": "auto_edit"}.get(runtime_mode, "default")


def model_options(config_options: list) -> list[dict]:
    model = next((o for o in config_options or [] if o.get("id") == "model"), None)
    if not model or model.get("type", "select") != "select":
        return []
    rows = []
    for entry in model.get("options") or []:
        rows.extend([entry] if "value" in entry else entry.get("options") or [])
    return rows


def current_model(config_options: list) -> Optional[str]:
    model = next((o for o in config_options or [] if o.get("id") == "model"), None)
    return (model or {}).get("currentValue")


# ── permission requests ──────────────────────────────────────────────────────────────────────────

def is_question(request: dict) -> bool:
    """Native questions share the permission method, but their choices are not approvals."""
    return str((request.get("toolCall") or {}).get("toolCallId", "")).startswith("interaction_")


def permission_option(request: dict, allowed: bool) -> Optional[str]:
    kind = "allow_once" if allowed else "reject_once"
    option = next((o for o in request.get("options") or [] if o.get("kind") == kind
                   and str(o.get("optionId", "")).strip()), None)
    return option["optionId"] if option else None


def question_label(option: dict) -> str:
    label = str(option.get("name") or "").strip() or option.get("optionId", "")
    return label if len(label) <= 512 else label[:509] + "..."


def question_answer(request: dict, value) -> Optional[str]:
    """Option id for an answer given as an option id or a unique label (``makeAntigravityUserInputResponse``)."""
    value = value[0] if isinstance(value, list) and value else value
    if not isinstance(value, str):
        return None
    options = request.get("options") or []
    exact = next((o for o in options if o.get("optionId") == value), None)
    if exact:
        return exact["optionId"]
    labelled = [o for o in options if question_label(o) == value]
    return labelled[0]["optionId"] if len(labelled) == 1 else None


# ── tool calls (normalizeAntigravityToolCall) ────────────────────────────────────────────────────

def _first(*values):
    return next((v for v in values if isinstance(v, str) and v.strip()), None)


def bound(text: str, limit: int = TOOL_TEXT_LIMIT) -> str:
    return text if len(text) <= limit else "[Earlier output truncated]\n\n" + text[-limit:]


def tool_command(call: dict) -> Optional[str]:
    raw_in = call.get("rawInput") if isinstance(call.get("rawInput"), dict) else {}
    raw_out = call.get("rawOutput") if isinstance(call.get("rawOutput"), dict) else {}
    return _first(raw_in.get("CommandLine"), raw_in.get("command_line"), raw_in.get("commandLine"),
                  raw_in.get("command"), raw_out.get("commandLine"), raw_out.get("command_line"))


SUBAGENT_TITLES = ("Running start_subagent", "Run start_subagent?")
INVOKE_TITLES = ("Running invoke_subagent", "Run invoke_subagent?")


def subagent_prompts(call: dict) -> list[str]:
    """The tasks of an ``invoke_subagent`` call (``Subagents: [{Prompt, Role, …}]``)."""
    raw = call.get("rawInput") if isinstance(call.get("rawInput"), dict) else {}
    return [str(s.get("Prompt") or s.get("prompt") or "").strip() for s in raw.get("Subagents") or raw.get("subagents") or []
            if isinstance(s, dict) and (s.get("Prompt") or s.get("prompt"))]


def tool_name(call: dict) -> str:
    if call.get("title") in SUBAGENT_TITLES:
        return "subagent"
    if call.get("title") in INVOKE_TITLES:
        return "invoke_subagent"
    kind = call.get("kind") or ("execute" if tool_command(call) else None)
    return {"execute": "shell", "edit": "edit", "read": "read", "search": "search", "fetch": "fetch",
            "delete": "delete", "move": "move", "think": "think"}.get(kind) or bound(call.get("title") or "tool", 80)


def tool_input(call: dict) -> dict:
    raw = call.get("rawInput")
    inputs = dict(raw) if isinstance(raw, dict) else ({"input": raw} if raw is not None else {})
    if command := tool_command(call):
        inputs = {"command": command, **{k: v for k, v in inputs.items()
                                          if k not in ("CommandLine", "command_line", "commandLine", "command")}}
    if call.get("title") and "title" not in inputs:
        inputs["title"] = bound(call["title"], 512)
    return inputs


def tool_output(call: dict) -> str:
    raw = call.get("rawOutput")
    if isinstance(raw, dict):
        text = _first(raw.get("combinedOutput"), raw.get("combined_output"))
        code = raw.get("exitCode", raw.get("exit_code"))
        if text is not None or code is not None:
            return bound((text or "") + (f"\n[exit code {code}]" if code not in (None, 0) else "")).strip()
    if isinstance(raw, str) and raw.strip():
        return bound(raw.strip())
    parts = []
    for entry in call.get("content") or []:
        inner = entry.get("content") if entry.get("type") == "content" else None
        if isinstance(inner, dict) and inner.get("type") == "text":
            parts.append(inner.get("text", ""))
        elif entry.get("type") == "diff":
            parts.append(f"Edited {entry.get('path', '')}")
    if parts:
        return bound("\n".join(parts))
    if raw is not None:
        return bound(json.dumps(raw, ensure_ascii=False, default=str))
    return ""
