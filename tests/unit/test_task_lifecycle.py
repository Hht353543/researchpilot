"""Lifecycle invariants: legal transitions, deadlines and cancellation."""

from __future__ import annotations

import threading
import time

import pytest

from researchpilot.lifecycle import (
    LifecycleCancelled,
    LifecycleTimedOut,
    TaskLifecycle,
)


def test_first_terminal_transition_wins() -> None:
    lifecycle = TaskLifecycle(timeout_s=10)
    assert lifecycle.transition("running")
    assert lifecycle.transition("completed")
    assert not lifecycle.transition("cancelled")
    assert not lifecycle.transition("failed")
    assert lifecycle.state == "completed"


@pytest.mark.parametrize(
    ("start", "target", "allowed"),
    [
        ("pending", "running", True),
        ("pending", "cancelled", True),
        ("pending", "timed_out", True),
        ("pending", "completed", False),
        ("running", "completed", True),
        ("running", "failed", True),
        ("running", "cancelled", True),
        ("running", "timed_out", True),
        ("completed", "running", False),
        ("cancelled", "completed", False),
        ("timed_out", "completed", False),
    ],
)
def test_only_declared_transitions_are_accepted(start: str, target: str, allowed: bool) -> None:
    lifecycle = TaskLifecycle(timeout_s=10)
    if start == "running":
        assert lifecycle.transition("running")
    elif start != "pending":
        assert lifecycle.transition("running")
        assert lifecycle.transition(start)  # type: ignore[arg-type]
    assert lifecycle.transition(target) is allowed  # type: ignore[arg-type]


def test_cancel_checkpoint_stops_new_work() -> None:
    lifecycle = TaskLifecycle(timeout_s=10)
    assert lifecycle.transition("running")
    assert lifecycle.cancel()
    with pytest.raises(LifecycleCancelled):
        lifecycle.checkpoint()


def test_deadline_checkpoint_stops_new_work() -> None:
    lifecycle = TaskLifecycle(timeout_s=0.01)
    assert lifecycle.transition("running")
    time.sleep(0.02)
    with pytest.raises(LifecycleTimedOut):
        lifecycle.checkpoint()
    assert lifecycle.state == "timed_out"


@pytest.mark.parametrize(
    ("left", "right"),
    [
        ("completed", "cancelled"),
        ("completed", "timed_out"),
        ("failed", "cancelled"),
    ],
)
def test_racing_terminal_transitions_have_exactly_one_winner(left: str, right: str) -> None:
    lifecycle = TaskLifecycle(timeout_s=10)
    assert lifecycle.transition("running")
    barrier = threading.Barrier(3)
    outcomes: list[bool] = []

    def transition(target: str) -> None:
        barrier.wait()
        outcomes.append(lifecycle.transition(target))  # type: ignore[arg-type]

    first = threading.Thread(target=transition, args=(left,))
    second = threading.Thread(target=transition, args=(right,))
    first.start()
    second.start()
    barrier.wait()
    first.join(timeout=2)
    second.join(timeout=2)

    assert sorted(outcomes) == [False, True]
    assert lifecycle.state in {left, right}
