"""Regression tests for workbench session ownership and review transitions."""

from pathlib import Path

import pytest

from crewline.domain.errors import InvalidStateTransitionError
from crewline.domain.models import RunStatus, TaskStatus
from crewline.engine.service import CrewlineService
from crewline.storage.sqlite_store import SQLiteStore
from crewline.workspace.terminal_sessions import TerminalSessions


def _claim(store: SQLiteStore, service: CrewlineService, task_id: str, launch_id: str | None):
    return store.claim_task_for_run(
        task_id, "mock-dev", "mock", "mock-v1", service.logs_dir, launch_id=launch_id
    )[1]


def test_refresh_recovers_only_interrupted_workbench_run(tmp_path: Path, monkeypatch) -> None:
    service = CrewlineService(tmp_path / ".crewline")
    service.init_workspace()
    live = service.create_task("Live session", assigned_agent="mock-dev")
    dead = service.create_task("Interrupted session", assigned_agent="mock-dev")
    external = service.create_task("Ordinary CLI run", assigned_agent="mock-dev")
    store = SQLiteStore(service.db_path)
    live_run = _claim(store, service, live.id, "live")
    dead_run = _claim(store, service, dead.id, "dead")
    _claim(store, service, external.id, None)

    sessions = TerminalSessions(service)
    monkeypatch.setattr(sessions, "is_alive", lambda task_id: task_id == live.id)
    running, recovered = sessions.refresh()

    assert running == {live.id: live_run}
    assert recovered == [dead.id]
    assert service.get_task_detail(external.id).task.status == TaskStatus.IN_PROGRESS
    assert service.get_task_detail(dead.id).task.status == TaskStatus.TODO
    assert service.get_workbench_run("dead").status == RunStatus.FAILED

    next_run = _claim(store, service, dead.id, "retry")
    assert not service.recover_workbench_run(dead_run)
    assert service.get_workbench_run("retry") == next_run
    assert service.get_task_detail(dead.id).task.status == TaskStatus.IN_PROGRESS


def test_running_card_cannot_be_rejected(tmp_path: Path) -> None:
    service = CrewlineService(tmp_path / ".crewline")
    service.init_workspace()
    task = service.create_task("Work in progress", assigned_agent="mock-dev")
    store = SQLiteStore(service.db_path)
    _claim(store, service, task.id, "active")

    with pytest.raises(InvalidStateTransitionError):
        service.reject_task(task.id, "Wait for the deliverable")

    detail = service.get_task_detail(task.id)
    assert detail.task.status == TaskStatus.IN_PROGRESS
    assert detail.runs[-1].status == RunStatus.RUNNING
    assert not detail.reviews
