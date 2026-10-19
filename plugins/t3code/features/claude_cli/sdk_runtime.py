"""A persistent official SDK/installed-CLI session with synchronous host event delivery."""

from __future__ import annotations

import asyncio
import contextvars
import logging
import queue
import threading
import time
import uuid

from agent.native_runtime import NativeAgentRuntime

from .options import bridge_mode, hermes_executable, native_options
from .projection import Projector


PLAN_APPROVE = "Approve plan and start"
PLAN_REVISE = "Keep planning"


def _prompt(message):
    if isinstance(message, str):
        return message
    parts = []
    for part in message:
        if part.get("type") == "text":
            parts.append({"type": "text", "text": part.get("text", "")})
        elif part.get("type") == "image_url":
            url = part.get("image_url", {})
            url = url.get("url", "") if isinstance(url, dict) else url
            if not url.startswith("data:") or ";base64," not in url:
                raise ValueError("Claude CLI accepts attached base64 images; download remote images first.")
            header, data = url.split(";base64,", 1)
            parts.append({"type": "image", "source": {
                "type": "base64", "media_type": header[5:], "data": data}})
        else:
            raise ValueError(f"Unsupported native input part: {part.get('type')}")

    async def content():
        yield {"type": "user", "message": {"role": "user", "content": parts}}
    return content()


class ClaudeRuntime(NativeAgentRuntime):
    """The worker task owns connect/disconnect's AnyIO cancel scope for the entire session."""

    def __init__(self, *, cwd, state, settings, environment):
        self.cwd, self.state, self.settings = cwd, state, settings
        self.environment = environment
        self._commands = queue.Queue()
        from .background import BackgroundMailbox
        self._background = BackgroundMailbox(self)
        self._route_events = None
        self._foreground_done = None
        self._native_idle = None
        self._reader_task = None
        self._ready = threading.Event()
        self._busy = threading.Lock()
        self._closed = False
        self._loop = None
        self._worker_task = None
        self._client = None
        self._pending = {}
        self._tasks = {}
        self._active_events = None
        self._worker_error = None
        self._interrupt_pending = threading.Event()
        self.commands = None  # Claude Code's own slash commands, once connected (Hermes's slash menu)
        context = contextvars.copy_context()
        self._thread = threading.Thread(target=lambda: context.run(self._thread_main),
                                        name="claude-cli-runtime", daemon=True)
        self._thread.start()
        self._background.start()

    def _thread_main(self):
        try:
            asyncio.run(self._worker())
        except BaseException as exc:
            self._worker_error = exc
        finally:
            self._ready.set()
            if self._active_events is not None:
                self._active_events.put({"type": "worker_error", "error": str(self._worker_error or "Native worker stopped")})
                self._active_events.put(None)

    async def _worker(self):
        from claude_agent_sdk import ClaudeSDKClient, HookMatcher
        self._loop = asyncio.get_running_loop()
        self._worker_task = asyncio.current_task()
        options = native_options(self.settings, self.environment, self.cwd, self.state)
        options.can_use_tool = self._permission
        # Python SDK permission callbacks require a streaming-input hook to keep stdin open.
        async def keep_open(_data, _tool_use_id, _context):
            return {"continue_": True}
        options.hooks = {"PreToolUse": [HookMatcher(matcher=None, hooks=[keep_open])]}
        self._attach_bridge(options)
        from .t3_bridge import server as t3_server
        if (t3 := t3_server()) is not None:
            options.mcp_servers = {**(options.mcp_servers if isinstance(options.mcp_servers, dict) else {}), "t3": t3}
        from . import cup_bridge
        try:
            cup = cup_bridge.server()
        except Exception:
            # Optional: a broken computer-use bridge must not take the native session down with it.
            logging.getLogger("t3code.claude").warning("computer-use bridge unavailable", exc_info=True)
            cup = None
        if cup is not None:
            options.mcp_servers = {**(options.mcp_servers if isinstance(options.mcp_servers, dict) else {}),
                                   cup_bridge.SERVER: cup}
            options.allowed_tools = [*(getattr(options, "allowed_tools", None) or []), *cup_bridge.preapproved()]
        from contextlib import AsyncExitStack
        from .model_selection import context_policy, launch_model
        policy = context_policy("default", self.settings)
        state = dict(self.state)
        self._live_state = state
        active_model = options.model
        async with AsyncExitStack() as stack:
            client = await stack.enter_async_context(ClaudeSDKClient(options))
            self._client = client
            try:
                from .commands import remember
                self.commands = remember(self.settings, self.cwd, await client.get_server_info())
            except Exception:
                pass  # the slash menu falls back to the cached list
            self._native_idle = asyncio.Event()
            self._native_idle.set()
            self._reader_task = asyncio.create_task(self._read_messages(client, state))
            self._ready.set()
            try:
                while True:
                    command = await asyncio.to_thread(self._commands.get)
                    if command is None:
                        break
                    message, model, events = command
                    self._active_events = events
                    try:
                        # User input can win host admission while a native wake is pending.
                        while (prior := self._background.take()) is not None:
                            events.put({"type": "prior_turn", "events": prior.events})
                            await self._native_idle.wait()
                        if self._worker_error:
                            raise RuntimeError(str(self._worker_error))
                        selected_policy = context_policy(model, self.settings)
                        if selected_policy != policy:
                            self._reader_task.cancel()
                            await asyncio.gather(self._reader_task, return_exceptions=True)
                            self._client = None
                            await stack.aclose()
                            updated = native_options(self.settings, self.environment, self.cwd, state, model=model)
                            updated.can_use_tool, updated.hooks = options.can_use_tool, options.hooks
                            updated.mcp_servers, updated.allowed_tools = options.mcp_servers, getattr(options, "allowed_tools", None)
                            client = await stack.enter_async_context(ClaudeSDKClient(updated))
                            self._client = client
                            policy, active_model = selected_policy, updated.model
                            self._reader_task = asyncio.create_task(self._read_messages(client, state))
                        selected_model = launch_model(model, policy)
                        if selected_model != active_model:
                            await client.set_model(selected_model)
                            active_model = selected_model
                        self._foreground_done = self._loop.create_future()
                        self._route_events = events
                        self._native_idle.clear()
                        await client.query(_prompt(message))
                        if self._interrupt_pending.is_set():
                            await client.interrupt()
                        await self._foreground_done
                    except Exception as exc:
                        events.put({"type": "worker_error", "error": str(exc)})
                        break
                    finally:
                        self._interrupt_pending.clear()
                        if cup is not None:
                            await asyncio.to_thread(cup_bridge.turn_ended)
                        events.put(None)
                        self._active_events = None
            finally:
                self._reader_task.cancel()
                await asyncio.gather(self._reader_task, return_exceptions=True)
                self._client = None

    def _attach_bridge(self, options):
        """Hermes profile delegation (bridge.py): Claude can start tasks on other Hermes profiles."""
        mode = bridge_mode(self.settings)
        if mode == "off":
            return
        try:
            hermes = hermes_executable(self.settings)
            from hermes_constants import get_hermes_home
            from .bridge import SERVER, TaskRegistry, build_server
            registry = TaskRegistry(get_hermes_home() / "cache" / "claude-cli" / "tasks", hermes,
                                    dict(self.environment), self.cwd,
                                    lambda: getattr(self, "_live_state", self.state).get("session_id", ""))
            server, preapproved = build_server(registry, mode)
        except Exception:
            # Optional: a broken bridge must not take the native session down with it.
            logging.getLogger("t3code.claude").warning("profile bridge unavailable", exc_info=True)
            return
        self._registry = registry
        options.mcp_servers = {**(options.mcp_servers if isinstance(options.mcp_servers, dict) else {}), SERVER: server}
        options.allowed_tools = [*(getattr(options, "allowed_tools", None) or []), *preapproved]

    # A background Bash, Monitor or subagent outlives the turn that started it; the thread is not
    # done while one is running, because its result wakes the conversation again.
    TASK_MAX_AGE = 6 * 3600

    def _track_task(self, message):
        from claude_agent_sdk.types import (TERMINAL_TASK_STATUSES, TaskNotificationMessage,
                                            TaskStartedMessage, TaskUpdatedMessage)
        if isinstance(message, TaskStartedMessage):
            self._tasks[message.task_id] = time.time()
        elif isinstance(message, (TaskNotificationMessage, TaskUpdatedMessage)):
            status = message.status if isinstance(message, TaskNotificationMessage) else (
                message.status or (message.patch or {}).get("status"))
            if status in TERMINAL_TASK_STATUSES:
                self._tasks.pop(message.task_id, None)

    def background_task_count(self):
        """Native tasks still running plus wake receipts not yet delivered; 0 once everything is settled."""
        cutoff = time.time() - self.TASK_MAX_AGE
        for task_id, started in list(self._tasks.items()):
            if started < cutoff:
                self._tasks.pop(task_id, None)
        return len(self._tasks) + len(self._background.pending)

    def _route(self, event):
        if self._route_events is None:
            self._route_events = self._background.create()
            self._native_idle.clear()
        self._route_events.put(event)

    async def _read_messages(self, client, state):
        # One reader per process, even while Hermes is idle. receive_response() strands
        # native monitor/subagent/background replies after the first terminal result.
        projector = Projector(self.cwd, str(self.settings.get("config_directory") or ""))
        try:
            async for message in client.receive_messages():
                self._track_task(message)
                native_id = getattr(message, "session_id", None)
                if native_id:
                    state.update(session_id=native_id, config_directory=projector.config_directory)
                if getattr(message, "subtype", None) == "compact_boundary" and state.get("session_id"):
                    # Hermes's context meter counts the CLI's compactions; persisted with the native state.
                    state["compactions"] = int(state.get("compactions") or 0) + 1
                    self._route({"type": "session", "state": {"session_id": state["session_id"],
                                                              "compactions": state["compactions"]}})
                for event in projector.project(message):
                    if event["type"] == "session":
                        state.update(event["state"])
                        state.pop("fork_from", None)
                        state.pop("fork_at", None)
                    self._route(event)
                    if event["type"] == "result":
                        origin = getattr(message, "origin", None) or {}
                        foreground = self._route_events is self._active_events and self._active_events is not None
                        if foreground and origin.get("kind") == "task-notification":
                            continue
                        if foreground:
                            self._foreground_done.set_result(None)
                        else:
                            self._route_events.put(None)
                        self._route_events = None
                        self._native_idle.set()
            raise RuntimeError("Claude Code stream ended")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._worker_error = exc
            self._tasks.clear()
            self._route({"type": "worker_error", "error": str(exc)})
            self._route_events.put(None)
            self._native_idle.set()
            if self._foreground_done is not None and not self._foreground_done.done():
                self._foreground_done.set_exception(exc)
            self._commands.put(None)

    async def _permission(self, name, inputs, _context):
        from claude_agent_sdk import PermissionResultAllow, PermissionResultDeny
        if self._closed or self._interrupt_pending.is_set():
            return PermissionResultDeny(message="Task stopped.", interrupt=True)
        request_id = uuid.uuid4().hex
        future = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        event = {"type": "request", "id": request_id, "kind": "permission", "tool": name, "input": inputs}
        if name == "AskUserQuestion":
            event["kind"] = "question"
            event["questions"] = [{"qid": f"q{i}", "question": q["question"],
                "choices": [o["label"] for o in q.get("options", [])], "multi_select": bool(q.get("multiSelect"))}
                for i, q in enumerate(inputs.get("questions", []))]
        elif name == "ExitPlanMode":
            event["kind"] = "question"
            event["questions"] = [{"qid": "q0", "question": f"Claude proposes this plan:\n\n{inputs.get('plan', '')}",
                                   "choices": [PLAN_APPROVE, PLAN_REVISE], "multi_select": False}]
        self._route(event)
        try:
            response = await future
        finally:
            self._pending.pop(request_id, None)
        if name == "ExitPlanMode":
            return self._plan_decision(response, inputs)
        if not response.get("allowed"):
            return PermissionResultDeny(message=response.get("message") or "Permission denied.")
        updated = dict(inputs)
        if name == "AskUserQuestion":
            answers = (response.get("answer") or {}).get("answers") or {}
            updated["answers"] = {q["question"]: ", ".join(answers[f"q{i}"]) if isinstance(answers[f"q{i}"], list) else answers[f"q{i}"]
                                  for i, q in enumerate(inputs.get("questions", []))
                                  if f"q{i}" in answers and answers[f"q{i}"] is not None}
        return PermissionResultAllow(updated_input=updated)

    def _plan_decision(self, response, inputs):
        from claude_agent_sdk import PermissionResultAllow, PermissionResultDeny, PermissionUpdate
        answer = ((response.get("answer") or {}).get("answers") or {}).get("q0")
        answer = ", ".join(answer) if isinstance(answer, list) else str(answer or "")
        if response.get("allowed") and answer == PLAN_APPROVE:
            mode = str(self.settings.get("permission_mode") or "default")
            mode = "default" if mode == "plan" else mode
            return PermissionResultAllow(updated_input=dict(inputs), updated_permissions=[
                PermissionUpdate(type="setMode", mode=mode, destination="session")])
        feedback = answer if answer and answer != PLAN_REVISE else ""
        return PermissionResultDeny(message="The user did not approve the plan yet. Stay in plan mode and revise it."
                                    + (f" User feedback: {feedback}" if feedback else ""))

    def run_turn(self, message, *, model):
        if self._closed:
            raise RuntimeError("Claude runtime is closed")
        if not self._busy.acquire(blocking=False):
            raise RuntimeError("This Claude conversation already has an active turn")
        try:
            self._interrupt_pending.clear()
            if not self._ready.wait(45):
                raise TimeoutError("Claude Code initialization did not finish within 45 seconds")
            if self._worker_error:
                raise RuntimeError(f"Claude Code initialization failed: {self._worker_error}")
            if not self._thread.is_alive():
                raise RuntimeError("Claude Code worker exited; resume the conversation explicitly")
            background = self._background.take(message) if isinstance(message, str) else None
            if background is not None:
                events = background.events
            else:
                events = queue.Queue()
                self._commands.put((message, model, events))
            yield from self._consume(events)
        finally:
            self._busy.release()

    def _consume(self, events):
        while True:
            try:
                event = events.get(timeout=1)
            except queue.Empty:
                if not self._thread.is_alive():
                    raise RuntimeError("Claude Code worker exited without finishing the turn")
                continue
            if event is None:
                return
            if event["type"] == "worker_error":
                raise RuntimeError(event["error"])
            if event["type"] == "prior_turn":
                yield from self._consume(event["events"])
            else:
                yield event

    def steer(self, text):
        """Deliver user text into the live native turn (priority "now"); False when no turn is running."""
        client, loop = self._client, self._loop
        if (not text or not text.strip() or client is None or loop is None or not self._busy.locked()
                or self._interrupt_pending.is_set()):
            return False

        async def send():
            async def one():
                yield {"type": "user", "message": {"role": "user", "content": text.strip()},
                       "parent_tool_use_id": None, "priority": "now"}
            await client.query(one())
            return True
        try:
            return bool(asyncio.run_coroutine_threadsafe(send(), loop).result(timeout=10))
        except Exception:
            logging.getLogger("t3code.claude").debug("steer failed", exc_info=True)
            return False

    def context_usage(self):
        """Claude Code's own /context reading for Hermes's context meter; None until connected."""
        client, loop = self._client, self._loop
        if self._closed or client is None or loop is None or not loop.is_running():
            return None
        from .projection import context_snapshot
        return context_snapshot(asyncio.run_coroutine_threadsafe(client.get_context_usage(), loop).result(timeout=8))

    def respond(self, request_id, response):
        def complete():
            future = self._pending.get(request_id)
            if future is not None and not future.done():
                future.set_result(response)
        self._loop.call_soon_threadsafe(complete)

    def interrupt(self):
        self._interrupt_pending.set()
        client, loop = self._client, self._loop
        if client is not None and loop is not None and loop.is_running():
            def deny_pending():
                for future in self._pending.values():
                    if not future.done():
                        future.set_result({"allowed": False, "message": "Task stopped."})
            loop.call_soon_threadsafe(deny_pending)
            # Do not wait: Stop must stay responsive while the SDK drains an interrupted turn.
            asyncio.run_coroutine_threadsafe(client.interrupt(), loop)

    def close(self):
        if self._closed:
            return
        self._closed = True
        self._background.close()
        self.interrupt()
        self._commands.put(None)
        self._thread.join(timeout=8)
        if self._thread.is_alive():
            # Cancel the owning task so SDK disconnect exits its own AnyIO scope.
            if self._worker_task is not None and self._loop.is_running():
                self._loop.call_soon_threadsafe(self._worker_task.cancel)
            self._thread.join(timeout=5)
        if self._thread.is_alive():
            raise RuntimeError("Claude runtime did not shut down cleanly")
