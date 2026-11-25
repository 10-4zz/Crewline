"""Tests for the Crewline task card finite state machine."""

import pytest

from crewline.domain.errors import CardAlreadyRunningError, InvalidStateTransitionError
from crewline.domain.models import TaskStatus
from crewline.domain.state_machine import can_transition, ensure_transition


@pytest.mark.parametrize(
    ("src", "dst", "expected"),
    [
        (TaskStatus.TODO, TaskStatus.IN_PROGRESS, True),
        (TaskStatus.IN_PROGRESS, TaskStatus.IN_REVIEW, True),
        (TaskStatus.IN_PROGRESS, TaskStatus.TODO, True),
        (TaskStatus.IN_REVIEW, TaskStatus.DONE, True),
        (TaskStatus.IN_REVIEW, TaskStatus.TODO, True),
        (TaskStatus.TODO, TaskStatus.IN_REVIEW, False),
        (TaskStatus.TODO, TaskStatus.DONE, False),
        (TaskStatus.IN_PROGRESS, TaskStatus.IN_PROGRESS, False),
        (TaskStatus.IN_PROGRESS, TaskStatus.DONE, False),
        (TaskStatus.DONE, TaskStatus.TODO, False),
        (TaskStatus.DONE, TaskStatus.IN_PROGRESS, False),
    ],
)
def test_state_transition_matrix(src: TaskStatus, dst: TaskStatus, expected: bool) -> None:
    assert can_transition(src, dst) is expected


def test_duplicate_start_raises_card_already_running() -> None:
    with pytest.raises(CardAlreadyRunningError):
        ensure_transition("task-001", TaskStatus.IN_PROGRESS, TaskStatus.IN_PROGRESS)


def test_illegal_transition_raises_invalid_state_error() -> None:
    with pytest.raises(InvalidStateTransitionError):
        ensure_transition("task-001", TaskStatus.TODO, TaskStatus.DONE)
