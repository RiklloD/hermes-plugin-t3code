"""Claude's model identity and native context policy, shared by catalog and launch."""
from __future__ import annotations

import re


def split_model(model: str) -> tuple[str, str]:
    match = re.search(r"\[(200k|1m)\]$", model, re.IGNORECASE)
    return (model[:match.start()], match[1].lower()) if match else (model, "auto")


def native_context(model: str) -> int:
    """Recognized first-party native windows; unknown/custom IDs stay conservative."""
    base, context = split_model(model)
    if context == "200k":
        return 200_000
    if context == "1m":
        return 1_000_000
    match = re.fullmatch(r"claude-(opus|sonnet|haiku|fable)-(\d+)(?:-(\d+))?(?:-\d{8})?", base)
    if not match:
        return 200_000
    family, major, minor = match[1], int(match[2]), int(match[3] or 0)
    large = (family == "fable" and major >= 5 or
             family == "opus" and (major > 4 or major == 4 and minor >= 7) or
             family == "sonnet" and major >= 5 or
             family == "haiku" and (major > 5 or major == 5 and minor >= 5))
    return 1_000_000 if large else 200_000


def context_policy(model: str, settings: dict | None = None) -> str:
    # Keep the original call contract while a backend may still hold cached provider modules.
    return split_model(model)[1]


def launch_model(model: str, policy: str) -> str | None:
    base, _ = split_model(model)
    if policy == "1m" and native_context(base) < 1_000_000:
        base += "[1m]"
    return None if base == "default" else base


def model_rows(native_rows: list[dict]) -> list[dict]:
    """Use the CLI's resolved IDs, not guessed release names or unversioned picker aliases."""
    grouped = {}
    for native in native_rows:
        alias = native.get("value")
        if not alias:
            continue
        model = native.get("resolvedModel") or alias
        prior = grouped.get(model)
        aliases = [*(prior or {}).get("aliases", []), alias]
        if prior and alias == "default":
            prior["aliases"] = aliases
            continue
        grouped[model] = {"id": model, "label": native.get("displayName", model),
                          "note": native.get("description", ""), "alias": alias, "aliases": aliases,
                          "context_window": native_context(model),
                          "supports_reasoning": bool(native.get("supportsEffort") or native.get("supportsAdaptiveThinking"))}
    rows = []
    for row in grouped.values():
        rows.append(row)
        if row["context_window"] == 1_000_000:
            rows.append({**row, "id": f"{split_model(row['id'])[0]}[200k]",
                         "label": f"{row['label']} · 200K", "context_window": 200_000,
                         "note": f"200K native context cap. {row['note']}"})
    return rows


class ModelCapabilities(dict):
    """Keep explicit-model metadata available before the picker has warmed its account catalog."""
    def get(self, model, default=None):
        if model in self:
            return super().get(model, default)
        base, context = split_model(model) if isinstance(model, str) else (model, "auto")
        if base in {"default", "opus", "sonnet", "haiku", "fable"} and base not in self:
            discover = getattr(self, "discover", None)
            if discover is not None:
                discover()
        if context != "auto" and base in self:
            return {**self[base], "context_window": 200_000 if context == "200k" else 1_000_000}
        if model in self:
            return super().get(model, default)
        if isinstance(model, str) and model.startswith("claude-"):
            return {"context_window": native_context(model), "supports_vision": True, "supports_tools": True}
        return default
