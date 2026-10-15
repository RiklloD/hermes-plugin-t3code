"""The turn plumbing the stdio-protocol providers (Muse Code, Antigravity) share.

Hermes consumes ``run_turn`` on its own thread; protocol callbacks arrive on the RPC reader thread
and only enqueue display events here. Requests that need an answer (permissions, questions) are
yielded as ``request`` events and answered through ``respond`` → the responder registered with them.
"""

from __future__ import annotations

import queue
import threading
from typing import Callable, Optional

from agent.native_runtime import NativeAgentRuntime

from . import identity


class TurnRuntime(NativeAgentRuntime):
    label = "Native runtime"

    def __init__(self, *, cwd, state, settings, environment):
        self.cwd, self.state, self.settings, self.environment = cwd, dict(state or {}), settings, environment
        self._busy = threading.Lock()
        self._events: Optional[queue.Queue] = None
        self._responders: dict[str, Callable[[dict], None]] = {}
        self._closed = False
        self._interrupted = threading.Event()
        self._soul_hash = self.state.get("soul_hash")  # the SOUL.md version this session last received

    # ── profile identity ──────────────────────────────────────────────────────────────────────

    def identity_block(self, *, command: bool = False):
        """``(text, hash)`` of the profile's SOUL.md when this session still needs it, else None.

        The agent has no system-prompt channel, so SOUL.md rides as a leading block of the turn: on
        the session's first turn and again whenever the file changed. A slash command turn is
        skipped (the agent parses the command from the prompt start); the next turn carries it.
        Confirm delivery with :meth:`identity_sent`.
        """
        if command:
            return None
        ident = identity.resolve(self.environment)
        if not ident or ident["hash"] == self._soul_hash:
            return None
        return ident["text"], ident["hash"]

    def identity_sent(self, digest: str) -> None:
        """Remember the delivery in memory and in the persisted native state (survives resume)."""
        self._soul_hash = digest
        if getattr(self, "session_id", None):
            self.emit({"type": "session", "state": {"session_id": self.session_id, "cwd": self.cwd,
                                                    "soul_hash": digest}})

    # ── subclass hooks ────────────────────────────────────────────────────────────────────────

    def begin_turn(self, message, model: str) -> None:
        """Start the native turn; events then arrive through :meth:`emit` until :meth:`end`."""
        raise NotImplementedError

    def interrupt_turn(self) -> None:
        raise NotImplementedError

    def shutdown(self) -> None:
        raise NotImplementedError

    # ── event plumbing ────────────────────────────────────────────────────────────────────────

    def emit(self, event: dict) -> None:
        events = self._events
        if events is not None:
            events.put(event)

    def end(self, result: Optional[dict] = None, error: Optional[str] = None) -> None:
        """Finish the active turn with a ``result`` event, or fail it with ``error``."""
        events = self._events
        if events is None:
            return
        events.put({"type": "worker_error", "error": error} if error else {"type": "result", **(result or {})})
        events.put(None)

    def ask(self, request_id: str, event: dict, responder: Callable[[dict], None]) -> None:
        self._responders[request_id] = responder
        self.emit({"type": "request", "id": request_id, **event})

    def run_turn(self, message, *, model):
        if self._closed:
            raise RuntimeError(f"{self.label} runtime is closed")
        if not self._busy.acquire(blocking=False):
            raise RuntimeError(f"This {self.label} conversation already has an active turn")
        events: queue.Queue = queue.Queue()
        self._events = events
        self._interrupted.clear()
        try:
            self.begin_turn(message, model)
            while True:
                event = events.get()
                if event is None:
                    break
                if event["type"] == "worker_error":
                    raise RuntimeError(event["error"])
                yield event
        finally:
            self._events = None
            self._responders.clear()
            self._busy.release()

    def respond(self, request_id, response):
        responder = self._responders.pop(request_id, None)
        if responder is not None:
            responder(response)

    def interrupt(self):
        self._interrupted.set()
        if self._events is not None:
            self.interrupt_turn()

    def close(self):
        if self._closed:
            return
        self._closed = True
        self.end(error=f"{self.label} session closed")
        self.shutdown()


def is_slash_command(text: str) -> bool:
    """``/name args`` (not ``//…`` and not a path like ``/usr/bin``): the agent runs it from the prompt start."""
    if not text.startswith("/") or text.startswith("//"):
        return False
    name = text[1:].partition(" ")[0].strip()
    return bool(name) and "/" not in name


def message_parts(message) -> tuple[str, list[tuple[str, str]]]:
    """``(text, [(media_type, base64)])`` from a Hermes user message (string or content parts)."""
    if isinstance(message, str):
        return message, []
    texts, images = [], []
    for part in message:
        kind = part.get("type")
        if kind == "text":
            texts.append(part.get("text", ""))
        elif kind == "image_url":
            url = part.get("image_url", {})
            url = url.get("url", "") if isinstance(url, dict) else url
            if not url.startswith("data:") or ";base64," not in url:
                raise ValueError("Attach images as base64 data; download remote images first.")
            header, data = url.split(";base64,", 1)
            images.append((header[5:], data))
        else:
            raise ValueError(f"Unsupported native input part: {kind}")
    return "\n".join(texts), images
