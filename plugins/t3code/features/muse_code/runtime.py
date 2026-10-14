"""One ``muse serve`` host per Hermes conversation, driven over MSP like T3 Code's ``MuseAdapterV2``.

Muse owns the prompt, tools, agent loop, approvals and persisted session; Hermes shows a display
mirror. The session id is minted here (UUIDv7) and resumed with ``session/resume`` on later turns.
"""

from __future__ import annotations

import copy
import json
import threading

from t3code_shared import native_commands, orchestration
from t3code_shared.native_turns import TurnRuntime, message_parts
from t3code_shared.stdio_rpc import RpcError, StdioRpc, uuid7

from . import catalog, commands, protocol

def save_usage(usage):
    """Keep the last subscription window for the Usage page; Muse itself keeps none on disk."""
    if not isinstance(usage, dict) or not isinstance(usage.get("window"), dict):
        return
    import json
    from t3code_shared.hermes_data import muse_usage_path
    try:
        path = muse_usage_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(usage))
        tmp.replace(path)
    except OSError:
        pass


INFORMATIONAL = {"session/contextUsage", "session/tokenUsage", "session/todoListChanged", "turn/retryScheduled"}


class MuseRuntime(TurnRuntime):
    label = "Muse Code"

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.runtime_mode = str(self.settings.get("runtime_mode") or "approval-required")
        if self.runtime_mode not in ("approval-required", "full-access"):
            raise ValueError("muse_runtime_mode must be approval-required or full-access")
        self.rpc = None
        self.session_id = self.state.get("session_id")
        self.session_model = None
        self.skills = None  # skill/list rows (Hermes's slash menu), refetched after skill/changed
        self._lock = threading.RLock()  # serialises reader-thread notifications with turn state
        self._turn = None
        self.hermes_session = ""  # the Hermes chat of the latest turn (orchestrator tools, subagents)
        self.mcp = None

    # ── host ──────────────────────────────────────────────────────────────────────────────────

    def _command(self, method, params, timeout=30):
        return self.rpc.request(method, {"sessionId": self.session_id, **params, "commandId": uuid7()}, timeout)

    def _ensure_host(self, model):
        if self.rpc is not None and self.rpc.alive:
            return
        env = protocol.muse_environment(self.environment)
        self.rpc = StdioRpc([catalog.executable(self.settings), *protocol.serve_args(runtime_mode=self.runtime_mode)],
                            cwd=self.cwd, env=env, name="Muse Code", on_notification=self._notification,
                            # Presentation receipts only; decisions travel as commands.
                            on_request=lambda request_id, *_: self.rpc.respond(request_id, {}),
                            on_exit=lambda reason: self.end(error=reason))
        init = self.rpc.request("initialize", protocol.initialize_params(), timeout=20)
        if ((init or {}).get("schema") or {}).get("version") != 1:
            raise RuntimeError("Muse SDK returned an unsupported protocol envelope version.")
        config = {}
        self.mcp = self.mcp or orchestration.mcp_endpoint(lambda: self.hermes_session)
        if self.mcp and "sessionMcp" in ((init or {}).get("grantedCapabilities") or []):
            # Muse defaults to "required", which fails the whole run when the tools cannot be
            # reached. The agent should still work without them (T3 Code's choice).
            config = {"config": {"mcpServers": {"t3-code": {
                "transport": "streamableHttp", "mode": "optional", "url": self.mcp[0],
                "headers": {"Authorization": f"Bearer {self.mcp[1]}"}}}}}
        self.rpc.notify("initialized")
        mode = protocol.approval_mode(self.runtime_mode)
        try:
            save_usage((self.rpc.request("usage/read", {}, timeout=10) or {}).get("usage"))
        except Exception:
            pass
        if not self.session_id and self.state.get("fork_from"):
            # An Orchestrator fork: Muse copies the source's completed turns into a new, loaded session.
            # (Session MCP config can't be attached to it now; the next host start resumes with it.)
            result = self.rpc.request("session/fork", {"sessionId": self.state["fork_from"], "excludeItems": True,
                                                       "commandId": uuid7()}, 60) or {}
            self.session_id = (result.get("session") or {}).get("sessionId")
            if not self.session_id:
                raise RuntimeError("Muse did not return the forked session")
            self._command("session/setApprovalMode", {"mode": mode})
        elif self.session_id:
            # Hermes already has the transcript; skip Muse's history payload.
            result = self._command("session/resume", {"excludeItems": True, **config})
            self._command("session/setApprovalMode", {"mode": mode})
        else:
            self.session_id = uuid7()
            start_model = self._resolve_model(model)
            result = self._command("session/start", {
                "workspaceRoot": self.cwd, "providerId": "meta", "approvalMode": mode, **config,
                **({"modelId": start_model} if start_model else {})})
        session = (result or {}).get("session") or {}
        if session.get("sessionId") != self.session_id:
            raise RuntimeError("Muse returned a different session identity")
        self.session_model = session.get("modelId")
        self.emit({"type": "session", "state": {"session_id": self.session_id, "cwd": self.cwd}})

    def _resolve_model(self, model):
        if model and model != "default":
            return model
        if catalog.default_model() is None:
            catalog.discover_models()
        return catalog.default_model()

    # ── turns ─────────────────────────────────────────────────────────────────────────────────

    def begin_turn(self, message, model):
        text, images = message_parts(message)
        parts = ([{"type": "text", "text": text}] if text else []) + [
            {"type": "image", "base64Data": data, "mediaType": media} for media, data in images]
        if not parts:
            raise ValueError("Muse needs text or an image")
        self.hermes_session = orchestration.current_session_key() or self.hermes_session
        self._ensure_host(model)
        target = self._resolve_model(model)
        if target and target != self.session_model:
            self._command("session/setModel", {"model": {"modelId": target, "providerId": "meta"}})
            self.session_model = target
        if (command := commands.parse(text)) is not None:
            name, args = command
            if name in commands.BUILTIN_NAMES:
                return self._builtin(name, args)
            if (selector := self._skill_selector(name)) is not None:
                # Muse resolves and expands its own skill; the images ride along.
                parts = [{"type": "skill", "selector": selector, **({"arguments": args} if args else {})}, *parts[1:]]
        effort = protocol.resolve_effort(catalog.cached_row(self.session_model or ""),
                                         str(self.settings.get("reasoning_effort") or ""))
        turn_id = uuid7()
        with self._lock:
            self._turn = self._new_turn(turn_id)
        try:
            result = self.rpc.request("turn/start", {
                "sessionId": self.session_id, "input": parts, "displayText": text or "Image attachment",
                # A resumed session keeps the root it was created with; pin it to this checkout.
                "workspaceRoots": [protocol.workspace_root(self.cwd)], "ifBusy": "queue",
                **({"reasoningEffort": effort} if effort else {}), "commandId": turn_id}, 60)
        except Exception:
            with self._lock:
                self._turn = None
            raise
        if (result or {}).get("turnId") != turn_id:
            raise RuntimeError("Muse admitted the turn under an unexpected identity")

    @staticmethod
    def _new_turn(turn_id):
        return {"id": turn_id, "joined": set(), "items": {}, "tools": set(), "done": set(),
                "streamed": set(), "asked": set(), "approval_current": {}, "usage": {}, "interrupt": False}

    # ── Muse's own slash commands (Hermes's slash menu → features/muse_code/commands.py) ──────

    def _skill_selector(self, name):
        if self.skills is None:
            try:
                entries = (self.rpc.request("skill/list", {"sessionId": self.session_id}, 15) or {}).get("skills")
                self.skills = commands.skill_rows(entries)
                native_commands.put(commands.PROVIDER, self.cwd, self.skills)
            except Exception:
                return None
        return next((row["name"] for row in self.skills if row["name"].lower() == name.lower()), None)

    def _builtin(self, name, args):
        def reply(text):
            # Muse answered (even "no"): a rejected command is a reply, not a failed turn.
            self.end({"text": text, "usage": {}, "api_calls": 0, "interrupted": False, "error": None})

        try:
            if name == "compact":
                result = self._command("session/compact", {}) or {}
                return reply("Muse is compacting this conversation." if result.get("status") != "noop"
                             else f"Nothing to compact ({result.get('reason') or 'noop'}).")
            if name == "rename":
                if not args:
                    return reply("Usage: /rename <name>")
                result = self._command("session/rename", {"name": args}) or {}
                return reply(f"Muse session renamed to {result.get('name') or args}.")
            verb = args.lower() if args.lower() in ("clear", "pause", "resume") else "set"
            if verb == "set" and not args:
                return reply("Usage: /goal <objective> | clear | pause | resume")
            # Setting or resuming a goal can wake a Muse turn: own it from its first event and stream it.
            with self._lock:
                self._turn = self._new_turn(None)
            try:
                result = self._command(f"goal/{verb}", {"objective": args} if verb == "set" else {}) or {}
            except Exception:
                with self._lock:
                    self._turn = None
                raise
            with self._lock:
                if result.get("turnId"):
                    self._turn["id"] = result["turnId"]
                    self._turn["joined"].discard(result["turnId"])
                    return None
                self._turn = None
            return reply({"set": "Goal set.", "clear": "Goal cleared.", "pause": "Goal paused.",
                          "resume": "Goal resumed."}[verb])
        except Exception as exc:
            reason = str(exc).rsplit("rejected: ", 1)[-1]
            return reply(f"/{name}: " + {"missing_run": "nothing to compact yet",
                                         "missing_goal": "no goal is set"}.get(reason, reason))

    def interrupt_turn(self):
        turn = self._turn
        if not turn or self.rpc is None:
            return
        turn["interrupt"] = True

        def stop():
            # Muse runs a joined turn first, so Stop must end it too.
            for turn_id in [*turn["joined"], turn["id"]]:
                try:
                    self._command("turn/interrupt", {"turnId": turn_id})
                except Exception:
                    pass
        threading.Thread(target=stop, name="muse-interrupt", daemon=True).start()

    def shutdown(self):
        orchestration.revoke((self.mcp or (None, None))[1])
        if self.rpc is not None:
            self.rpc.close()

    # ── notifications (reader thread) ─────────────────────────────────────────────────────────

    def _notification(self, method, params):
        if method == "usage/changed":
            return save_usage(params)
        if method == "skill/changed":
            self.skills = None
            return
        try:
            with self._lock:
                self._handle(method, params)
        except Exception as exc:
            if method not in INFORMATIONAL:
                self.end(error=f"Unreadable Muse {method}: {exc}")

    def _owns(self, turn, turn_id):
        return turn_id is None or turn_id == turn["id"] or turn_id in turn["joined"]

    def _handle(self, method, params):
        turn = self._turn
        if params.get("sessionId") not in (None, self.session_id):
            return
        if method in ("item/started", "item/updated", "item/completed") and \
                (params.get("item") or {}).get("kind") in ("subagent", "workflow"):
            # Children outlive the turn that started them: report them whenever Muse updates them.
            self._report_children(params["item"])
        if turn is None:
            return
        if method == "view/gap":
            raise RuntimeError("Muse delivery gap requires session recovery")
        if method == "turn/started" and params.get("turnId") not in (None, turn["id"]):
            turn["joined"].add(params["turnId"])  # Muse started a turn on its own (a finished workflow)
            return
        if method in ("item/started", "item/updated", "item/completed"):
            item = params.get("item") or {}
            if not self._owns(turn, item.get("turnId")):
                return
            previous = turn["items"].get(item.get("itemId"))
            if previous and previous.get("revision", 0) >= item.get("revision", 0):
                return
            turn["items"][item["itemId"]] = item
            self._publish(turn, item)
        elif method == "item/delta":
            item = turn["items"].get(params.get("itemId"))
            if not item or item.get("status") != "inProgress":
                return
            field, delta = params.get("field") or "text", params.get("delta") or ""
            if item.get("kind") == "agentMessage" and field == "text":
                item["text"] = (item.get("text") or "") + delta
                turn["streamed"].add(item["itemId"])
                self.emit({"type": "text_delta", "text": delta})
            elif item.get("kind") == "reasoning" and (field == "text" or field.startswith("summary.")):
                turn["streamed"].add(item["itemId"])
                self.emit({"type": "reasoning_delta", "text": delta})
            elif field in ("output", "visibleOutput"):
                item["visibleOutput"] = (item.get("visibleOutput") or "") + delta
        elif method in ("approval/requested", "approval/updated"):
            self._approval(turn, params)
        elif method == "userInput/requested":
            self._question(turn, params)
        elif method == "session/tokenUsage" and self._owns(turn, params.get("turnId")):
            usage, total = params.get("usage") or {}, turn["usage"]
            cached = int(usage.get("cacheReadTokens", usage.get("cachedTokens")) or 0)
            written = int(usage.get("cacheWriteTokens") or 0)
            total["input_tokens"] = total.get("input_tokens", 0) + max(0, int(params.get("promptTokens") or 0) - cached - written)
            total["output_tokens"] = total.get("output_tokens", 0) + int(usage.get("outputTokens") or 0)
            total["cache_read_input_tokens"] = total.get("cache_read_input_tokens", 0) + cached
            total["cache_creation_input_tokens"] = total.get("cache_creation_input_tokens", 0) + written
            total["api_calls"] = total.get("api_calls", 0) + 1
        elif method == "session/todoListChanged":
            steps = [f"{'✓' if i.get('status') == 'completed' else '•'} {i.get('text', '').strip()}"
                     for i in (params.get("items") or []) if i.get("text", "").strip() and i.get("status") != "cancelled"]
            if steps:
                self.emit({"type": "status", "text": "Muse todo list:\n" + "\n".join(steps)})
        elif method == "turn/retryScheduled":
            self.emit({"type": "status", "text": f"Muse is retrying this turn (attempt {params.get('nextAttempt')} "
                                                 f"of {params.get('maxAttempts')}): {params.get('reason')}"})
        elif method == "turn/completed" and params.get("turnId") == turn["id"]:
            self._complete(turn, params)

    # ── native subagents → Orchestrator V2 (Subagents frame) ─────────────────────────────────

    def _control(self, method, params):
        try:
            self._command(method, params)
            return True
        except Exception:
            return False

    def _report_children(self, item):
        if not self.hermes_session:
            return
        status = protocol.item_status(item)
        if item.get("kind") == "subagent":
            child = item.get("subagentId") or item["itemId"]
            # A subagent with a ready result has finished its work even while Muse keeps it open.
            if status == "running" and item.get("controlStatus") == "resultReady":
                status = "completed"
            result = item.get("result") or {}
            orchestration.report_subagent(self.hermes_session, {
                "id": f"muse_{child}", "goal": item.get("objective") or item.get("role") or "Muse agent",
                "model": " · ".join(x for x in ("Muse Code", item.get("role")) if x),
                "status": {"cancelled": "interrupted"}.get(status, status),
                "summary": result.get("text") or result.get("summary") or item.get("failureReason"),
                "transcript": f"Role: {item['role']}" if item.get("role") else None},
                stop=lambda: self._control("subagent/stop", {"subagentId": child, "reason": "Stopped from Hermes"}),
                steer=lambda text: self._control("subagent/sendMessage", {"subagentId": child, "body": text}))
            return
        run_id = item.get("workflowRunId")
        stop = (lambda: self._control("workflow/cancel", {"workflowRunId": run_id})) if run_id else None
        children = item.get("children") or []
        if not children:
            orchestration.report_subagent(self.hermes_session, {
                "id": f"muse_wf_{item['itemId']}", "goal": item.get("entryId") or "Workflow", "model": "Muse Code · workflow",
                "status": {"cancelled": "interrupted"}.get(status, status),
                "summary": item.get("finalSummary") or item.get("failureReason")}, stop=stop)
            return
        for child in children:
            terminal = child.get("terminal")
            child_status = ("running" if not terminal else "interrupted" if terminal == "cancelled" else
                            "failed" if terminal != "completed" or child.get("failureKind") else "completed")
            orchestration.report_subagent(self.hermes_session, {
                "id": f"muse_wf_{item['itemId']}_{child['childId']}",
                "goal": child.get("label") or child["childId"], "model": f"Muse Code · {item.get('entryId') or 'workflow'}",
                "status": child_status,
                "summary": child.get("failureReason") or (item.get("finalSummary") if terminal else None)}, stop=stop)

    def _publish(self, turn, item):
        item_id, kind, status = item["itemId"], item.get("kind"), protocol.item_status(item)
        if kind == "agentMessage":
            if status != "running" and item_id not in turn["done"]:
                turn["done"].add(item_id)
                if item.get("text"):
                    if item_id not in turn["streamed"]:
                        self.emit({"type": "text_delta", "text": item["text"]})
                    self.emit({"type": "assistant", "text": item["text"], "reasoning": "", "tool_calls": []})
        elif kind == "reasoning":
            text = item.get("text") or "\n".join(item.get("summary") or [])
            if status != "running" and item_id not in turn["done"] and item_id not in turn["streamed"] and text:
                turn["done"].add(item_id)
                self.emit({"type": "reasoning_delta", "text": text})
        elif kind == "compaction":
            if status != "running" and item_id not in turn["done"]:
                turn["done"].add(item_id)
                self.emit({"type": "status", "text": "Muse compacted its context."})
        elif protocol.is_tool(item):
            name, inputs = protocol.tool_name(item), protocol.tool_input(item)
            if item_id not in turn["tools"]:
                turn["tools"].add(item_id)
                import json
                self.emit({"type": "assistant", "text": "", "reasoning": "", "tool_calls": [
                    {"id": item_id, "type": "function", "function": {
                        "name": name, "arguments": json.dumps(inputs, ensure_ascii=False)}}]})
                self.emit({"type": "tool_start", "id": item_id, "name": name, "input": inputs})
            if status != "running" and item_id not in turn["done"]:
                turn["done"].add(item_id)
                self.emit({"type": "tool_result", "id": item_id, "name": name, "input": inputs,
                           "content": protocol.tool_output(item), "is_error": status == "failed"})

    def _approval(self, turn, approval):
        approval_id = approval.get("approvalId")
        if not approval_id or turn["interrupt"] or not self._owns(turn, approval.get("turnId")) \
                and approval.get("subagentOrigin") is None:
            return
        if approval.get("status") in ("approved", "denied", "aborted", "cancelled", "completed"):
            turn["approval_current"].pop(approval_id, None)
            return
        choices = protocol.approval_choices(approval)
        if not choices:
            return
        # Muse advances requirements under one approval ID; dedupe stages, not the action.
        approval = copy.deepcopy(approval)
        choices = protocol.approval_choices(approval)
        requirement = json.dumps(approval.get("currentRequirementId"), sort_keys=True)
        key = ("approval", approval_id, requirement)
        if key in turn["asked"]:
            return
        turn["asked"].add(key)
        turn["approval_current"][approval_id] = key
        subject = approval.get("subject") or {}

        def decide(response):
            with self._lock:
                if self._turn is not turn or turn["interrupt"] or \
                        turn["approval_current"].get(approval_id) != key:
                    return
            choice = choices.get("accept" if response.get("allowed") else "decline")
            if choice is None:
                return
            threading.Thread(target=self._send_decision, args=(approval, choice, turn, key), daemon=True).start()

        self.ask(f"approval:{approval_id}:{requirement}", {
            "kind": "permission", "tool": approval.get("toolName") or subject.get("kind") or "tool",
            "input": {"request": protocol.approval_prompt(approval),
                      **{k: v for k, v in subject.items() if k in ("kind", "access", "command", "path", "host", "target")}}},
            decide)

    def _send_decision(self, approval, choice, turn, key):
        with self._lock:
            if self._turn is not turn or turn["interrupt"] or \
                    turn["approval_current"].get(approval["approvalId"]) != key:
                return
        try:
            self._command("approval/decide", {"approvalId": approval["approvalId"],
                                               "requirementId": approval.get("currentRequirementId"),
                                               "choiceId": choice["choiceId"]})
        except (RpcError, TimeoutError, ConnectionError):
            pass  # Muse may already have settled it (for example its own judge approved)

    def _question(self, turn, request):
        user_input_id = request.get("userInputId")
        if not user_input_id or user_input_id in turn["asked"]:
            return
        turn["asked"].add(user_input_id)
        questions = [{"qid": q["id"], "question": q.get("question") or q.get("header") or "Question",
                      "choices": [o.get("label") for o in q.get("options") or []],
                      "multi_select": (q.get("selection") or {}).get("mode") == "multiple"}
                     for q in request.get("questions") or []]

        def answer(response):
            answers = ((response.get("answer") or {}).get("answers") or {}) if response.get("allowed") else {}

            def send():
                try:
                    if answers:
                        self._command("userInput/answer", {"userInputId": user_input_id,
                                                            "answers": protocol.question_answers(request, answers)})
                    else:
                        self.interrupt_turn()
                except Exception as exc:
                    self.emit({"type": "status", "text": f"Muse question was not answered: {exc}"})
            threading.Thread(target=send, daemon=True).start()

        self.ask(f"question:{user_input_id}", {"kind": "question", "tool": "request_user_input",
                                               "questions": questions}, answer)

    def _complete(self, turn, result):
        terminal = result.get("terminal")
        interrupted = terminal == "cancelled" and turn["interrupt"]
        error = None if terminal == "completed" or interrupted else (
            (result.get("error") or {}).get("message") or result.get("reason") or f"Muse turn {terminal}")
        usage = dict(turn["usage"])
        calls = usage.pop("api_calls", 1)
        texts = [i.get("text", "") for i in turn["items"].values() if i.get("kind") == "agentMessage" and i.get("text")]
        self._turn = None
        self.end({"text": texts[-1] if texts else "", "usage": usage, "api_calls": calls,
                  "interrupted": interrupted, "error": error})
