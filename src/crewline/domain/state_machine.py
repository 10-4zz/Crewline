"""Explicit finite state machine for Crewline task cards."""

from crewline.domain.errors import CardAlreadyRunningError, InvalidStateTransitionError
from crewline.domain.models import TaskStatus

# State transitions:
#   todo        -> in_progress (start run)
#   in_progress -> in_review   (run succeeded)
#   in_progress -> todo        (run failed)
#   in_review   -> done        (human approved)
#   in_review   -> todo        (human rejected with comment)
VALID_TRANSITIONS: dict[TaskStatus, frozenset[TaskStatus]] = {
    TaskStatus.TODO: frozenset({TaskStatus.IN_PROGRESS}),
    TaskStatus.IN_PROGRESS: frozenset({TaskStatus.IN_REVIEW, TaskStatus.TODO}),
    TaskStatus.IN_REVIEW: frozenset({TaskStatus.DONE, TaskStatus.TODO}),
    TaskStatus.DONE: frozenset(),
}


def can_transition(current: TaskStatus, target: TaskStatus) -> bool:
    """Return True if transitioning from current to target is legal."""
    return target in VALID_TRANSITIONS[current]


def ensure_transition(task_id: str, current: TaskStatus, target: TaskStatus) -> None:
    """Validate a state transition or raise a descriptive domain error."""
    if current == TaskStatus.IN_PROGRESS and target == TaskStatus.IN_PROGRESS:
        raise CardAlreadyRunningError(
            f"任务卡 {task_id} 当前正在处理中（{current.label_zh}），不能重复启动。"
        )
    if not can_transition(current, target):
        raise InvalidStateTransitionError(
            f"任务卡 {task_id} 不允许从「{current.label_zh}({current.value})」"
            f"流转到「{target.label_zh}({target.value})」。"
        )
