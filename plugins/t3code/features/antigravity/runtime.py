"""One official Antigravity ACP agent per Hermes conversation, driven like T3 Code's
``AntigravityAdapterV2`` (on ``AcpSessionRuntime``): authenticate eagerly, prefer ``session/resume``
over replaying history, cancel by waiting for the prompt to settle.
"""

from __future__ import annotations

import json
import threading

from t3code_shared import orchestration
from t3code_shared.native_turns import TurnRuntime, message_parts
from t3code_shared.stdio_rpc import RpcError, StdioRpc

from . import catalog, commands, protocol

DEFAULT_MODELS = {"default", "antigravity-default"}


class AntigravityRuntime(TurnRuntime):
    label = "Antigravity"

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.runtime_mode = str(self.settings.get("runtime_mode") or "approval-required")
        if self.runtime_mode not in ("approval-required", "auto-accept-edits", "full-access"):
            raise ValueError("antigravity_runtime_mode must be approval-required, auto-accept-edits or full-access")
        self.auth = protocol.auth_config(self.settings)
        self.rpc = None
        self.session_id = self.state.get("session_id")
        self.config_options: list = []
        self.modes: dict = {}
        self._lock = threading.RLock()
        self._turn = None
        self.hermes_session = ""  # the Hermes chat of the latest turn (orchestrator tools, subagents)
        self.mcp = None
        self.commands = None  # the agent's available_commands_update (Hermes's slash menu)

    # ── host ──────────────────────────────────────────────────────────────────────────────────

    def _stderr(self, line):
        if line.startswith(protocol.AUTH_URL_PREFIX):
            url = line[len(protocol.AUTH_URL_PREFIX):].strip()
            self.emit({"type": "status", "text": f"Sign in to Antigravity in your browser: {url}"})
            if protocol.uses_browser(self.auth["method"]) and url.startswith("https://accounts.google.com/"):
                import webbrowser
                webbrowser.open(url)

    def _ensure_host(self):
        if self.rpc is not None and self.rpc.alive:
            return
        profile = catalog.profile_dir(self.settings)
        protocol.prepare_profile(profile, self.auth)
        found = catalog.runtime(self.settings)
        self.rpc = StdioRpc(protocol.argv(found), cwd=self.cwd, name="Antigravity",
                            env=protocol.environment(self.environment, profile, found, self.auth),
                            on_notification=self._notification, on_request=self._server_request,
                            on_stderr=self._stderr, on_exit=lambda reason: self.end(error=reason))
        init = self.rpc.request("initialize", catalog.initialize_params(file_system=False), timeout=90) or {}
        # A missing Google sign-in opens the browser here; wait for the user to finish it.
        self.rpc.request("authenticate", {"methodId": self.auth["method"]},
                         timeout=600 if protocol.uses_browser(self.auth["method"]) else 60)
        servers = []
        self.mcp = self.mcp or orchestration.mcp_endpoint(lambda: self.hermes_session)
        if self.mcp and ((init.get("agentCapabilities") or {}).get("mcpCapabilities") or {}).get("http"):
            servers = [{"type": "http", "name": "t3", "url": self.mcp[0],
                        "headers": [{"name": "Authorization", "value": f"Bearer {self.mcp[1]}"}]}]
        payload = {"cwd": self.cwd, "mcpServers": servers}
        if self.session_id:
            result = self.rpc.request("session/resume", {"sessionId": self.session_id, **payload}, timeout=120)
        else:
            result = self.rpc.request("session/new", payload, timeout=120)
            self.session_id = (result or {})["sessionId"]
        result = result or {}
        self.config_options = result.get("configOptions") or []
        self.modes = result.get("modes") or {}
        catalog.remember_models(self.settings, self.config_options)
        self._apply_mode(protocol.permission_mode(self.runtime_mode))
        self.emit({"type": "session", "state": {"session_id": self.session_id, "cwd": self.cwd}})

    def _set_config(self, config_id, value):
        response = self.rpc.request("session/set_config_option",
                                    {"sessionId": self.session_id, "configId": config_id, "value": value}, timeout=60)
        if isinstance(response, dict) and response.get("configOptions"):
            self.config_options = response["configOptions"]

    def _apply_mode(self, mode_id):
        option = next((o for o in self.config_options if o.get("category") == "mode" and o.get("type") == "select"), None)
        if option is not None:
            if option.get("currentValue") != mode_id:
                self._set_config(option["id"], mode_id)
        elif self.modes and self.modes.get("currentModeId") != mode_id:
            self.rpc.request("session/set_mode", {"sessionId": self.session_id, "modeId": mode_id}, timeout=60)
            self.modes["currentModeId"] = mode_id

    def _apply_model(self, model):
        """``applyAntigravityAcpModelSelection``: the default alias keeps the agent's choice."""
        if not model or model in DEFAULT_MODELS:
            return
        if model == protocol.current_model(self.config_options):
            return
        if not any(o.get("value") == model for o in protocol.model_options(self.config_options)):
            raise ValueError(f"Antigravity model '{model}' is unavailable for this Google account. "
                             "Select an available model.")
        self._set_config("model", model)

    # ── turns ─────────────────────────────────────────────────────────────────────────────────

    def begin_turn(self, message, model):
        text, images = message_parts(message)
        blocks = ([{"type": "text", "text": text}] if text.strip() else []) + [
            {"type": "image", "data": data, "mimeType": media} for media, data in images]
        if not blocks:
            raise ValueError("A turn requires text or supported attachments.")
        self.hermes_session = orchestration.current_session_key() or self.hermes_session
        self._ensure_host()
        self._apply_model(model)
        with self._lock:
            self._turn = {"text": [], "reasoning": [], "tools": {}, "started": set(), "done": set(),
                          "requests": {}, "final": [], "context": None}
        future = self.rpc.call("session/prompt", {"sessionId": self.session_id, "prompt": blocks})
        future.add_done_callback(self._prompt_done)

    def _prompt_done(self, future):
        with self._lock:
            turn, self._turn = self._turn, None
            if turn is None:
                return
            self._flush_text(turn)
            # Antigravity idles its subagent batches when the turn ends; an idle batch is not proof
            # that every child succeeded, but it is no longer running.
            for call_id, call in (turn.get("subagents") or {}).items():
                if call.get("status") not in ("completed", "failed"):
                    self._report_subagent(turn, call_id, call, status="completed")
            for request_id in list(turn["requests"]):
                self.rpc.respond(request_id, {"outcome": {"outcome": "cancelled"}})
        try:
            result = future.result() or {}
        except RpcError as exc:
            message = str(exc)
            if exc.kind in ("auth_required",) or exc.code == -32000:
                message = f"{message}. {protocol.SIGN_IN_REQUIRED}"
            return self.end({"text": "", "usage": {}, "api_calls": 1, "interrupted": False, "error": message})
        except Exception as exc:
            return self.end(error=str(exc))
        reason = result.get("stopReason")
        usage = {}
        if isinstance(result.get("usage"), dict):  # ACP's optional prompt usage
            u = result["usage"]
            cached = int(u.get("cachedReadTokens") or 0)
            usage = {"input_tokens": max(0, int(u.get("inputTokens") or 0) - cached),
                     "output_tokens": int(u.get("outputTokens") or 0), "cache_read_input_tokens": cached,
                     "cache_creation_input_tokens": int(u.get("cachedWriteTokens") or 0)}
        self.end({"text": "".join(turn["final"]).strip(), "usage": usage, "api_calls": 1,
                  "interrupted": reason == "cancelled",
                  "error": None if reason in ("end_turn", "cancelled") else f"Antigravity stopped: {reason}"})

    def interrupt_turn(self):
        if self.rpc is None or self._turn is None:
            return
        with self._lock:
            turn = self._turn
            for request_id in list((turn or {}).get("requests", {})):
                self.rpc.respond(request_id, {"outcome": {"outcome": "cancelled"}})
                turn["requests"].pop(request_id, None)
        try:
            # wait-for-prompt: the pending session/prompt settles with stopReason "cancelled".
            self.rpc.notify("session/cancel", {"sessionId": self.session_id})
        except ConnectionError:
            pass

    def shutdown(self):
        orchestration.revoke((self.mcp or (None, None))[1])
        if self.rpc is not None:
            self.rpc.close()

    # ── notifications and requests (reader thread) ────────────────────────────────────────────

    def _flush_text(self, turn, tool_calls=()):
        text, reasoning = "".join(turn["text"]), "".join(turn["reasoning"])
        turn["text"], turn["reasoning"] = [], []
        if text or reasoning or tool_calls:
            self.emit({"type": "assistant", "text": text, "reasoning": reasoning, "tool_calls": list(tool_calls)})

    def _notification(self, method, params):
        if method != "session/update":
            return
        update = params.get("update") or {}
        kind = update.get("sessionUpdate")
        if kind == "available_commands_update":
            # Sent right after session/new answers, possibly before session_id is stored.
            if self.session_id in (None, params.get("sessionId")):
                self.commands = commands.remember(update.get("availableCommands"))
            return
        if params.get("sessionId") != self.session_id:
            return
        with self._lock:
            turn = self._turn
            if kind in ("config_option_update", "config_options_update") and update.get("configOptions"):
                self.config_options = update["configOptions"]
                catalog.remember_models(self.settings, self.config_options)
            elif kind == "current_mode_update" and self.modes:
                self.modes["currentModeId"] = update.get("currentModeId")
            if turn is None:
                return
            if kind == "agent_message_chunk":
                text = (update.get("content") or {}).get("text") or ""
                if text:
                    turn["text"].append(text)
                    turn["final"].append(text)
                    self.emit({"type": "text_delta", "text": text})
            elif kind == "agent_thought_chunk":
                text = (update.get("content") or {}).get("text") or ""
                if text:
                    turn["reasoning"].append(text)
                    self.emit({"type": "reasoning_delta", "text": text})
            elif kind in ("tool_call", "tool_call_update"):
                self._tool(turn, update)
            elif kind == "plan":
                steps = [f"{'✓' if e.get('status') == 'completed' else '•'} {e.get('content', '').strip()}"
                         for e in update.get("entries") or [] if str(e.get("content", "")).strip()]
                if steps:
                    self.emit({"type": "status", "text": "Antigravity plan:\n" + "\n".join(steps)})

    def _tool(self, turn, update):
        call_id = update.get("toolCallId")
        if not call_id:
            return
        call = turn["tools"].setdefault(call_id, {})
        call.update({k: v for k, v in update.items() if v is not None and k not in ("sessionUpdate", "_display")})
        # The display identity is fixed by the first update; later ones only carry progress.
        name, inputs = call.setdefault("_display", (protocol.tool_name(call), protocol.tool_input(call)))
        if call_id not in turn["started"]:
            turn["started"].add(call_id)
            turn["final"].clear()  # the answer is the text after the last tool call
            self._flush_text(turn, [{"id": call_id, "type": "function", "function": {
                "name": name, "arguments": json.dumps(inputs, ensure_ascii=False, default=str)}}])
            self.emit({"type": "tool_start", "id": call_id, "name": name, "input": inputs})
        if name == "invoke_subagent":
            turn["invoke"] = call  # the batch's tasks; the start_subagent call that follows runs them
        elif name == "subagent" and not (call.get("_meta") or {}).get("is_mcp_tool_call"):
            self._report_subagent(turn, call_id, call)
        if call.get("status") in ("completed", "failed") and call_id not in turn["done"]:
            turn["done"].add(call_id)
            self.emit({"type": "tool_result", "id": call_id, "name": name, "input": inputs,
                       "content": protocol.tool_output(call), "is_error": call.get("status") == "failed"})

    def _report_subagent(self, turn, call_id, call, status=None):
        """ACP 1.1.1 exposes subagents as ``start_subagent`` tools without child ids: one row per batch."""
        invoke = call.setdefault("_invoke", turn.get("invoke") or {})  # the batch this start belongs to
        raw = invoke.get("rawInput") if isinstance(invoke.get("rawInput"), dict) else {}
        prompts = protocol.subagent_prompts(invoke)
        goal = (prompts[0] if len(prompts) == 1 else f"{len(prompts)} subagents: " + " · ".join(prompts)) \
            if prompts else "Antigravity subagent batch"
        native = call.get("status")
        status = status or ("failed" if native == "failed" else "completed" if native == "completed" else "running")
        turn.setdefault("subagents", {})[call_id] = call
        output = call.get("rawOutput")
        orchestration.report_subagent(self.hermes_session, {
            "id": f"antigravity_{call_id}", "goal": protocol.bound(goal, 400), "model": "Antigravity · subagents",
            "status": status, "summary": protocol.bound(output.strip()) if isinstance(output, str) and output.strip() else None,
            "transcript": protocol.bound(json.dumps(raw, ensure_ascii=False, default=str), 4000) if raw else None})

    def _server_request(self, request_id, method, params):
        if method != "session/request_permission":
            self.rpc.respond(request_id, error={"code": -32601, "message": f"Unsupported method: {method}"})
            return
        with self._lock:
            turn = self._turn
            if turn is None:
                self.rpc.respond(request_id, {"outcome": {"outcome": "cancelled"}})
                return
            turn["requests"][request_id] = params
        tool_call = params.get("toolCall") or {}
        key = f"permission:{request_id}"

        if protocol.is_question(params):
            options = params.get("options") or []

            def answer(response):
                value = next(iter(((response.get("answer") or {}).get("answers") or {}).values()), None)
                option_id = protocol.question_answer(params, value) if response.get("allowed") else None
                self._settle(request_id, {"outcome": {"outcome": "selected", "optionId": option_id}}
                             if option_id else {"outcome": {"outcome": "cancelled"}})
            self.ask(key, {"kind": "question", "tool": "question", "questions": [{
                "qid": tool_call.get("toolCallId"), "question": tool_call.get("title") or "Choose an option.",
                "choices": [protocol.question_label(o) for o in options], "multi_select": False}]}, answer)
            return

        call = {**((self._turn or {}).get("tools", {}).get(tool_call.get("toolCallId")) or {}), **tool_call}

        def decide(response):
            option_id = protocol.permission_option(params, bool(response.get("allowed")))
            self._settle(request_id, {"outcome": {"outcome": "selected", "optionId": option_id}}
                         if option_id else {"outcome": {"outcome": "cancelled"}})
        self.ask(key, {"kind": "permission", "tool": protocol.tool_name(call), "input": protocol.tool_input(call)}, decide)

    def _settle(self, request_id, result):
        with self._lock:
            if self._turn is not None and self._turn["requests"].pop(request_id, None) is not None:
                self.rpc.respond(request_id, result)
