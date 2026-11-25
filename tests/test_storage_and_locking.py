"""Tests for SQLite persistence and single-card duplicate-start protection."""

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import threading

import pytest

from crewline.domain.errors import CardAlreadyRunningError
from crewline.domain.models import TaskStatus
from crewline.engine.service import CrewlineService
from crewline.storage.sqlite_store import SQLiteStore


def test_concurrent_claim_allows_only_one_starter(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "crewline.db")
    task = store.create_task("并发抢占测试", "验证原子状态锁", str(tmp_path), "mock-dev")

    barrier = threading.Barrier(6)
    results: list[str] = []
    errors: list[Exception] = []
    lock = threading.Lock()

    def worker() -> None:
        barrier.wait()
        try:
            _, run = store.claim_task_for_run(
                task.id, "mock-dev", "mock", "mock-v1", tmp_path / "logs"
            )
            with lock:
                results.append(run.id)
        except Exception as exc:  # noqa: BLE001
            with lock:
                errors.append(exc)

    with ThreadPoolExecutor(max_workers=6) as pool:
        futures = [pool.submit(worker) for _ in range(6)]
        for f in futures:
            f.result()

    assert len(results) == 1
    assert len(errors) == 5
    assert all(isinstance(err, CardAlreadyRunningError) for err in errors)
    assert store.get_task(task.id).status == TaskStatus.IN_PROGRESS


def test_running_same_card_twice_simultaneously_blocks_second(tmp_path: Path) -> None:
    service = CrewlineService(tmp_path / ".crewline")
    service.init_workspace()
    task = service.create_task(
        title="防止重复启动测试",
        description="[mock:delay=0.3] 模拟稍长任务",
        repo_path=tmp_path,
        assigned_agent="mock-dev",
    )

    outcomes = service.run_tasks([task.id, task.id], max_workers=2)
    started = [o for o in outcomes if o.started]
    blocked = [o for o in outcomes if not o.started]

    assert len(started) == 1
    assert started[0].succeeded is True
    assert len(blocked) == 1
    assert blocked[0].error is not None
    assert "不能重复启动" in blocked[0].error or "不允许从" in blocked[0].error
    assert service.get_task_detail(task.id).task.status == TaskStatus.IN_REVIEW
    assert len(service.get_task_detail(task.id).runs) == 1
