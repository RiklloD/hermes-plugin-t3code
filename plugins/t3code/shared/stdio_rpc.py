"""Newline-delimited JSON-RPC 2.0 over a child's stdio. Shared by the Muse Code (MSP) and
Antigravity (ACP) providers, which T3 Code drives through ``@muse-code/sdk`` and ``effect-acp``.

One reader thread per process. Responses resolve futures; notifications and server requests go to
the owner's callbacks on the reader thread, so callbacks must only enqueue. A server request is
answered later with :meth:`StdioRpc.respond` (a permission prompt can take minutes).
"""

from __future__ import annotations

import collections
import json
import os
import signal
import subprocess
import threading
import time
import uuid
from concurrent.futures import Future
from typing import Any, Callable, Optional


class RpcError(Exception):
    """A server-authored JSON-RPC error. ``kind`` is MSP's ``data.kind`` when present."""

    def __init__(self, method: str, error: dict):
        self.method, self.code = method, error.get("code")
        self.data = error.get("data") if isinstance(error.get("data"), dict) else {}
        self.kind = self.data.get("kind", "unknown")
        super().__init__(f"{method} failed: {error.get('message') or 'error'}")


def uuid7() -> str:
    """MSP command ids must be UUIDv7 (the SDK's ``createUuidV7Mint``)."""
    raw = bytearray(os.urandom(16))
    raw[0:6] = int(time.time() * 1000).to_bytes(6, "big")
    raw[6] = 0x70 | (raw[6] & 0x0F)
    raw[8] = (raw[8] & 0x3F) | 0x80
    return str(uuid.UUID(bytes=bytes(raw)))


class StdioRpc:
    def __init__(self, argv: list[str], *, cwd: Optional[str], env: dict, name: str,
                 on_notification: Callable[[str, dict], None],
                 on_request: Callable[[Any, str, dict], None] = lambda *_: None,
                 on_stderr: Callable[[str], None] = lambda _line: None,
                 on_exit: Callable[[str], None] = lambda _reason: None):
        self.name = name
        self._on_notification, self._on_request = on_notification, on_request
        self._on_stderr, self._on_exit = on_stderr, on_exit
        self._pending: dict[int, tuple[str, Future]] = {}
        self._next_id = 1
        self._lock = threading.Lock()
        self._write_lock = threading.Lock()
        self.stderr_tail: collections.deque[str] = collections.deque(maxlen=40)
        self.closed = False
        self.proc = subprocess.Popen(
            argv, cwd=cwd, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, start_new_session=os.name != "nt")
        threading.Thread(target=self._read_stdout, name=f"{name}-rpc", daemon=True).start()
        threading.Thread(target=self._read_stderr, name=f"{name}-stderr", daemon=True).start()

    # ── outgoing ──────────────────────────────────────────────────────────────────────────────

    def _write(self, frame: dict) -> None:
        line = (json.dumps(frame, ensure_ascii=False) + "\n").encode("utf-8")
        with self._write_lock:
            if self.closed or self.proc.stdin is None:
                raise ConnectionError(f"{self.name} connection is closed")
            self.proc.stdin.write(line)
            self.proc.stdin.flush()

    def call(self, method: str, params: Optional[dict] = None) -> Future:
        future: Future = Future()
        with self._lock:
            request_id = self._next_id
            self._next_id += 1
            self._pending[request_id] = (method, future)
        try:
            self._write({"jsonrpc": "2.0", "id": request_id, "method": method,
                         **({} if params is None else {"params": params})})
        except (OSError, ValueError, ConnectionError) as exc:
            with self._lock:
                self._pending.pop(request_id, None)
            future.set_exception(ConnectionError(f"{self.name} {method} failed: {exc}"))
        return future

    def request(self, method: str, params: Optional[dict] = None, timeout: float = 30) -> Any:
        try:
            return self.call(method, params).result(timeout)
        except TimeoutError:
            raise TimeoutError(f"{self.name} {method} timed out") from None

    def notify(self, method: str, params: Optional[dict] = None) -> None:
        self._write({"jsonrpc": "2.0", "method": method, **({} if params is None else {"params": params})})

    def respond(self, request_id: Any, result: Any = None, error: Optional[dict] = None) -> None:
        frame = {"jsonrpc": "2.0", "id": request_id}
        frame.update({"error": error} if error is not None else {"result": result if result is not None else {}})
        try:
            self._write(frame)
        except (OSError, ValueError, ConnectionError):
            pass

    # ── incoming ──────────────────────────────────────────────────────────────────────────────

    def _read_stdout(self) -> None:
        reason = "connection reached EOF"
        try:
            for raw in self.proc.stdout:
                try:
                    message = json.loads(raw)
                except ValueError:
                    # Antigravity prints its sign-in link outside the protocol; treat like stderr.
                    self._stderr_line(raw.decode("utf-8", "replace").rstrip())
                    continue
                if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
                    continue
                method, request_id = message.get("method"), message.get("id")
                if isinstance(method, str):
                    params = message.get("params") if isinstance(message.get("params"), dict) else {}
                    if request_id is not None:
                        self._on_request(request_id, method, params)
                    else:
                        self._on_notification(method, params)
                    continue
                with self._lock:
                    entry = self._pending.pop(request_id, None)
                if entry is None:
                    continue
                name, future = entry
                if "error" in message:
                    future.set_exception(RpcError(name, message["error"] or {}))
                else:
                    future.set_result(message.get("result"))
        except Exception as exc:  # a broken pipe or decode failure ends the connection
            reason = str(exc)
        finally:
            self._finish(reason)

    def _stderr_line(self, line: str) -> None:
        if line.strip():
            self.stderr_tail.append(line)
        try:
            self._on_stderr(line)
        except Exception:
            pass

    def _read_stderr(self) -> None:
        for raw in self.proc.stderr:
            self._stderr_line(raw.decode("utf-8", "replace").rstrip())

    def _finish(self, reason: str) -> None:
        with self._lock:
            pending, self._pending = list(self._pending.values()), {}
        was_closed, self.closed = self.closed, True
        detail = next((line for line in reversed(self.stderr_tail) if line.strip()), "")
        message = f"{self.name} exited unexpectedly ({reason})" + (f": {detail}" if detail else "")
        for _name, future in pending:
            if not future.done():
                future.set_exception(ConnectionError(message))
        if not was_closed:
            self._on_exit(message)

    @property
    def alive(self) -> bool:
        return not self.closed and self.proc.poll() is None

    def close(self, timeout: float = 10) -> None:
        self.closed = True
        try:
            if self.proc.stdin:
                self.proc.stdin.close()
        except OSError:
            pass
        try:
            self.proc.wait(timeout)
            return
        except subprocess.TimeoutExpired:
            pass
        for sig in (signal.SIGTERM, signal.SIGKILL if os.name != "nt" else signal.SIGTERM):
            try:
                if os.name != "nt":
                    os.killpg(self.proc.pid, sig)
                else:
                    self.proc.terminate()
                self.proc.wait(3)
                return
            except (ProcessLookupError, PermissionError, subprocess.TimeoutExpired, OSError):
                continue
