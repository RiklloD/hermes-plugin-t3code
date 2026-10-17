"""Runtime fixes to Hermes core that live here so a ``hermes update`` cannot wipe them.

Each fix checks that the core shape it relies on still exists and skips itself, with a warning,
when it does not; a fix must never be able to break a turn on a newer Hermes.

* ``lease_survives_sleep`` — a turn no longer dies with "Task stopped." after the Mac sleeps.
  The turn-lease refresher runs on a monotonic timer, which stops during macOS sleep, while the
  lease row expires on the wall clock. The first tick after wake found its authority already
  spent and hard-interrupted the turn without trying to renew. Stopping then protects nothing
  (the row is already reclaimable), so a late tick gets one write window and core's own
  holder-fenced UPDATE decides: it renews only if no successor took the row. Every failure path
  (successor holds it, lock persists, other error) still stops the turn exactly as before.
  Belongs upstream; delete this fix once core does it.
"""

from __future__ import annotations

import functools
import logging
import time

logger = logging.getLogger("t3code")

_MARK = "_t3code_lease_survives_sleep"


def lease_survives_sleep() -> bool:
    """Wrap ``DurableTurnLease.refresh_tick``; True when applied (or already applied)."""
    from agent import turn_facade_lease as core

    cls = getattr(core, "DurableTurnLease", None)
    original = getattr(cls, "refresh_tick", None)
    margin = getattr(core, "_REFRESH_EXPIRY_MARGIN_S", None)
    patience = getattr(core, "_REFRESH_WRITE_PATIENCE_S", None)
    if original is None or margin is None or patience is None:
        logger.warning("t3code: turn lease internals changed; sleep fix not applied")
        return False
    if getattr(original, _MARK, False):
        return True

    @functools.wraps(original)
    def refresh_tick(self):
        deadline = getattr(self, "_authority_deadline", None)
        if isinstance(deadline, (int, float)) and not self.stop.is_set():
            now = time.time()
            late = now - (deadline - margin)
            if late >= 0:
                logger.info("t3code: turn lease tick ran %.0fs late (host sleep?); renewing if still held: %s",
                            late, self._current_session_id())
                # Only a successful renewal keeps this; every failure path in core stops the turn.
                self._authority_deadline = now + margin + patience
        return original(self)

    setattr(refresh_tick, _MARK, True)
    cls.refresh_tick = refresh_tick
    return True


def apply_all() -> None:
    lease_survives_sleep()
