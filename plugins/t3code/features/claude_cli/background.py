"""Native responses outlive user turns. Queue them, then claim normal host turn admission.

Wake tokens are private mailbox receipts, never prompts sent to Claude. The host still owns
persistence, streaming, approvals and Stop; the native process keeps its own conversation.
"""
from __future__ import annotations

import contextvars
import logging
import queue
import sys
import threading
import uuid
from collections import OrderedDict
from dataclasses import dataclass, field

logger = logging.getLogger('t3code.claude')


@dataclass
class BackgroundTurn:
    token: str = field(default_factory=lambda: f'__claude_background__{uuid.uuid4().hex}')
    events: queue.Queue = field(default_factory=queue.Queue)


class BackgroundMailbox:
    def __init__(self, runtime):
        self.runtime = runtime
        self.lock = threading.RLock()
        self.pending = OrderedDict()
        self.ready = threading.Event()
        self.stopped = threading.Event()
        self.thread = None

    def create(self):
        turn = BackgroundTurn()
        with self.lock:
            self.pending[turn.token] = turn
        self.ready.set()
        return turn.events

    def take(self, token=None):
        with self.lock:
            if token is None:
                return self.pending.popitem(last=False)[1] if self.pending else None
            return self.pending.pop(token, None)

    def contains(self, token):
        with self.lock:
            return token in self.pending

    def start(self):
        # No host is needed by direct SDK clients/tests; they can consume mailbox receipts themselves.
        if 'tui_gateway.server' not in sys.modules:
            return
        context = contextvars.copy_context()
        self.thread = threading.Thread(target=lambda: context.run(self._dispatch),
                                       name='claude-background-delivery', daemon=True)
        self.thread.start()

    def _dispatch(self):
        while not self.stopped.is_set():
            self.ready.wait()
            if self.stopped.is_set():
                return
            with self.lock:
                token = next(iter(self.pending), None)
                if token is None:
                    self.ready.clear()
                    continue
            try:
                self._deliver(token)
            except Exception:
                logger.exception('Claude native background delivery failed; retaining receipt')
            # Admission can be refused while the original host turn is still settling, or after Stop.
            self.stopped.wait(0.1)

    def _deliver(self, token):
        srv = sys.modules.get('tui_gateway.server')
        if srv is None:
            return False
        with srv._sessions_lock:
            owners = [(sid, session) for sid, session in srv._sessions.items()
                      if getattr(session.get('agent'), '_native_runtime', None) is self.runtime]
        for sid, session in owners:
            with srv._session_profile_runtime_scope(session):
                with srv._session_turn_admission(session) as admitted:
                    if (not admitted or not self.contains(token)
                            or any(session.get(k) for k in ('running', '_closing', '_finalized',
                                                          '_turn_cancel_requested', 'queued_prompt', 'queued_prompts'))):
                        continue
                    session['running'] = True
                    generation = int(session.get('_queued_prompt_generation', 0))
                # Admission takes history_lock itself; never dispatch while holding that lock.
                # The queue generation fences a Stop between our claim and host admission.
                try:
                    accepted = bool(srv._run_prompt_submit(token, sid, session, token, display_kind='hidden',
                                                           queued_prompt_generation=generation, image_paths=[]))
                except Exception:
                    srv._notif_release_turn(session)
                    raise
                if not accepted:
                    srv._notif_release_turn(session)
                return accepted
        return False

    def close(self):
        self.stopped.set()
        self.ready.set()
        if self.thread is not None and self.thread is not threading.current_thread():
            self.thread.join(timeout=2)
