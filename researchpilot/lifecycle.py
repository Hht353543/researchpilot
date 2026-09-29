"""Small cooperative lifecycle primitive shared by tasks, models and tools."""

from __future__ import annotations

import threading
import time
from typing import Literal

LifecycleState = Literal["pending", "running", "completed", "failed", "cancelled", "timed_out"]
TerminalState = Literal["completed", "failed", "cancelled", "timed_out"]

TERMINAL_STATES: frozenset[str] = frozenset({"completed", "failed", "cancelled", "timed_out"})
_TRANSITIONS: dict[str, frozenset[str]] = {
    "pending": frozenset({"running", "cancelled", "timed_out"}),
    "running": frozenset({"completed", "failed", "cancelled", "timed_out"}),
    "completed": frozenset(),
    "failed": frozenset(),
    "cancelled": frozenset(),
    "timed_out": frozenset(),
}


class LifecycleInterrupt(BaseException):
    """Control-flow signal that ordinary agent fallback handlers must not swallow."""


class LifecycleCancelled(LifecycleInterrupt):
    """The task owner requested cancellation."""


class LifecycleTimedOut(LifecycleInterrupt):
    """The task-level wall-clock deadline expired."""


class TaskLifecycle:
    """Thread-safe state machine and cooperative cancellation/deadline token."""

    def __init__(self, *, timeout_s: float) -> None:
        self._state: LifecycleState = "pending"
        self._deadline = time.monotonic() + timeout_s
        self._lock = threading.Lock()

    @property
    def state(self) -> LifecycleState:
        with self._lock:
            return self._state

    @property
    def terminal(self) -> bool:
        return self.state in TERMINAL_STATES

    def transition(self, target: LifecycleState) -> bool:
        """Apply one legal transition; the first terminal transition wins."""
        with self._lock:
            if target not in _TRANSITIONS[self._state]:
                return False
            self._state = target
            return True

    def cancel(self) -> bool:
        with self._lock:
            if "cancelled" not in _TRANSITIONS[self._state]:
                return False
            self._state = "cancelled"
            return True

    def mark_timed_out(self) -> bool:
        with self._lock:
            if "timed_out" not in _TRANSITIONS[self._state]:
                return False
            self._state = "timed_out"
            return True

    def remaining(self) -> float:
        return max(self._deadline - time.monotonic(), 0.0)

    def checkpoint(self) -> None:
        """Raise outside ``Exception`` so agent fallback code cannot continue work."""
        state = self.state
        if state == "cancelled":
            raise LifecycleCancelled("task cancellation requested")
        if state == "timed_out":
            raise LifecycleTimedOut("task deadline exceeded")
        if state in {"completed", "failed"}:
            raise LifecycleInterrupt(f"task is already {state}")
        if self.remaining() <= 0:
            self.mark_timed_out()
            raise LifecycleTimedOut("task deadline exceeded")
