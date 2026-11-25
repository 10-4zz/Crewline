"""Regression tests for assign-on-create and soft delete/restore card management."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import sqlite3
import subprocess
import threading

from click.testing import CliRunner
import pytest

from crewline.cli import workbench as wb_module
from crewline.cli.main import cli
from crewline.domain.errors import (
    AgentNotFoundError,
    CardAlreadyRunningError,
    InvalidStateTransitionError,
)
from crewline.domain.models import RunStatus, TaskStatus
from crewline.engine.service import CrewlineService
from crewline.storage.sqlite_store import SQLiteStore
from crewline.workspace.terminal_sessions import TerminalSessions


class _NullCtx:
    def __enter__(self) -> None:
        return None

    def __exit__(self, *args: object) -> None:
        return None


class _FakeScreen:
    def __init__(self, rows: int = 30, cols: int = 110) -> None:
        self.rows = rows
        self.cols = cols
        self.lines: list[str] = []

    def erase(self) -> None:
        self.lines.clear()

    def getmaxyx(self) -> tuple[int, int]:
        return self.rows, self.cols

    def addstr(self, _y: int, _x: int, text: str, _attr: int = 0) -> None:
        self.lines.append(text)

    def refresh(self) -> None:
        return None


def _git(cwd: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
    )
    return proc.stdout.strip()


def _init_git_repo(repo_dir: Path) -> None:
    repo_dir.mkdir(parents=True, exist_ok=True)
    _git(repo_dir, "init")
    _git(repo_dir, "config", "user.name", "Crewline Card Test")
    _git(repo_dir, "config", "user.email", "card@crewline.local")
    (repo_dir / ".gitignore").write_text(".crewline/\n", encoding="utf-8")
    (repo_dir / "README.md").write_text("# Card Test Repo\n", encoding="utf-8")
    _git(repo_dir, "add", ".")
    _git(repo_dir, "commit", "-m", "initial commit")


def test_create_with_optional_agent_cli_and_workbench(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / ".crewline"
    service = CrewlineService(home)
    service.init_workspace()

    # Existing CLI `task create --agent` continues to work.
    runner = CliRunner()
    res = runner.invoke(
        cli,
        [
            "--home",
            str(home),
            "task",
            "create",
            "--title",
            "CLI 指派建卡",
            "--agent",
            "mock-dev",
            "--repo",
            str(tmp_path),
            "--json",
        ],
    )
    assert res.exit_code == 0, res.output
    created_cli = json.loads(res.output)
    assert created_cli["assigned_agent"] == "mock-dev"
    assert created_cli["deleted"] is False
    assert created_cli["deleted_at"] is None

    # Workbench `n` with optional agent selection (reuse agent picker) + reassign via `a`.
    wb = wb_module.Workbench(service)
    prompts = iter(["工作台直接指派卡", "填写描述"])
    monkeypatch.setattr(wb, "_prompt", lambda _label: next(prompts))
    monkeypatch.setattr(wb, "_prompt_agent", lambda _label: "mock-dev")
    wb.handle("n", None)

    card_assigned = service.get_task_detail(wb.selected_id or "").task
    assert card_assigned.title == "工作台直接指派卡"
    assert card_assigned.assigned_agent == "mock-dev"

    # Can still reassign via `a`.
    monkeypatch.setattr(wb, "_prompt_agent", lambda _label: "mock-qa")
    wb.handle("a", card_assigned)
    assert service.get_task_detail(card_assigned.id).task.assigned_agent == "mock-qa"

    # Workbench `n` pressing Enter skips agent assignment, then `a` assigns later.
    prompts_skip = iter(["工作台跳过指派卡", "稍后指派"])
    monkeypatch.setattr(wb, "_prompt", lambda _label: next(prompts_skip))
    monkeypatch.setattr(wb, "_prompt_agent", lambda _label: "")
    wb.handle("n", None)

    card_unassigned = service.get_task_detail(wb.selected_id or "").task
    assert card_unassigned.assigned_agent is None
    monkeypatch.setattr(wb, "_prompt_agent", lambda _label: "mock-dev")
    wb.handle("a", card_unassigned)
    assert service.get_task_detail(card_unassigned.id).task.assigned_agent == "mock-dev"

    # Invalid agent during `n` raises AgentNotFoundError and does not create a task.
    before_count = len(service.list_tasks())
    prompts_bad = iter(["非法 Agent 卡", "desc"])
    monkeypatch.setattr(wb, "_prompt", lambda _label: next(prompts_bad))
    monkeypatch.setattr(wb, "_prompt_agent", lambda _label: "nonexistent-agent")
    with pytest.raises(AgentNotFoundError):
        wb.handle("n", None)
    assert len(service.list_tasks()) == before_count


def test_soft_delete_and_restore_preserve_state_history_and_worktree(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    home = repo / ".crewline"
    service = CrewlineService(home)
    service.init_workspace()
    runner = CliRunner()

    task = service.create_task(
        title="可恢复删除完整生命周期",
        description="[mock:file=docs/keep.md] 验证历史与 worktree 保留",
        repo_path=repo,
        assigned_agent="mock-dev",
    )
    assert service.run_tasks([task.id])[0].succeeded is True
    service.reject_task(task.id, "补充细节")
    assert service.run_tasks([task.id])[0].succeeded is True

    before = service.get_task_detail(task.id)
    assert before.task.status == TaskStatus.IN_REVIEW
    assert len(before.runs) == 2
    assert len(before.reviews) == 1
    wt_path = Path(before.task.worktree_path or "")
    assert (wt_path / "docs/keep.md").exists()

    # Delete card via CLI.
    res_del = runner.invoke(cli, ["--home", str(home), "task", "delete", task.id])
    assert res_del.exit_code == 0, res_del.output
    assert "回收区" in res_del.output

    # Hidden from default list, visible in --deleted (text & JSON).
    res_active = runner.invoke(cli, ["--home", str(home), "task", "list", "--json"])
    assert res_active.exit_code == 0
    assert json.loads(res_active.output) == []

    res_deleted_txt = runner.invoke(cli, ["--home", str(home), "task", "list", "--deleted"])
    assert res_deleted_txt.exit_code == 0
    assert task.id in res_deleted_txt.output
    assert "待审核(in_review)" in res_deleted_txt.output

    res_deleted_json = runner.invoke(
        cli, ["--home", str(home), "task", "list", "--deleted", "--json"]
    )
    assert res_deleted_json.exit_code == 0
    deleted_items = json.loads(res_deleted_json.output)
    assert len(deleted_items) == 1
    item = deleted_items[0]
    assert item["id"] == task.id
    assert item["status"] == "in_review"
    assert item["assigned_agent"] == "mock-dev"
    assert item["deleted"] is True
    assert item["deleted_at"] is not None

    # `task show`, `logs`, and `diff` remain read-only accessible while deleted.
    res_show = runner.invoke(cli, ["--home", str(home), "task", "show", task.id])
    assert res_show.exit_code == 0
    assert "已删除" in res_show.output

    res_logs = runner.invoke(cli, ["--home", str(home), "logs", task.id])
    assert res_logs.exit_code == 0
    assert "docs/keep.md" in res_logs.output

    res_diff = runner.invoke(cli, ["--home", str(home), "diff", task.id])
    assert res_diff.exit_code == 0
    assert "docs/keep.md" in res_diff.output
    assert (wt_path / "docs/keep.md").exists()
    assert _git(repo, "branch", "--list", f"crewline/{task.id}")

    # Restore via CLI: keeps original ID, status, assignment, runs, reviews, worktree, branch.
    res_restore = runner.invoke(cli, ["--home", str(home), "task", "restore", task.id])
    assert res_restore.exit_code == 0, res_restore.output

    after = service.get_task_detail(task.id)
    assert after.task.id == task.id
    assert after.task.is_deleted is False
    assert after.task.deleted_at is None
    assert after.task.status == TaskStatus.IN_REVIEW
    assert after.task.assigned_agent == "mock-dev"
    assert after.task.worktree_path == str(wt_path)
    assert after.task.worktree_branch == f"crewline/{task.id}"
    assert len(after.runs) == 2
    assert len(after.reviews) == 1

    # Can approve after restore, and even delete/restore a done card preserving done status.
    approved, _ = service.approve_task(task.id, comment="恢复后通过")
    assert approved.status == TaskStatus.DONE
    service.delete_task(task.id)
    restored_done = service.restore_task(task.id)
    assert restored_done.status == TaskStatus.DONE


def test_running_card_cannot_be_deleted_and_deleted_card_blocks_operations(tmp_path: Path) -> None:
    home = tmp_path / ".crewline"
    service = CrewlineService(home)
    service.init_workspace()
    runner = CliRunner()

    # 1. Running card cannot be deleted.
    running_card = service.create_task("运行中卡", "desc", tmp_path, "mock-dev")
    store = SQLiteStore(service.db_path)
    store.claim_task_for_run(running_card.id, "mock-dev", "mock", "mock-v1", service.logs_dir)

    with pytest.raises(CardAlreadyRunningError):
        service.delete_task(running_card.id)
    res_del_running = runner.invoke(
        cli, ["--home", str(home), "task", "delete", running_card.id]
    )
    assert res_del_running.exit_code == 1
    assert service.get_task_detail(running_card.id).task.is_deleted is False

    # 2. Deleted todo card rejects assign, direct run, session start, and is skipped by run --all-todo.
    deleted_todo = service.create_task("已删待办卡", "[mock:file=del.md]", tmp_path, "mock-dev")
    active_todo = service.create_task("正常待办卡", "[mock:file=act.md]", tmp_path, "mock-qa")
    service.delete_task(deleted_todo.id)

    with pytest.raises(InvalidStateTransitionError):
        service.assign_task(deleted_todo.id, "mock-qa")

    direct_run = service.run_tasks([deleted_todo.id])[0]
    assert direct_run.started is False
    assert direct_run.succeeded is False
    assert direct_run.error is not None and "已删除" in direct_run.error

    with pytest.raises(InvalidStateTransitionError):
        TerminalSessions(service).start(deleted_todo.id)

    res_sess = runner.invoke(
        cli, ["--home", str(home), "session", "start", deleted_todo.id]
    )
    assert res_sess.exit_code == 1

    # Batch `run --all-todo` skips deleted_todo and runs only active_todo.
    res_batch = runner.invoke(cli, ["--home", str(home), "run", "--all-todo", "--json"])
    assert res_batch.exit_code == 0, res_batch.output
    batch_outcomes = json.loads(res_batch.output)
    assert [o["task_id"] for o in batch_outcomes] == [active_todo.id]
    assert batch_outcomes[0]["succeeded"] is True
    assert service.get_task_detail(deleted_todo.id).runs == []

    # 3. Deleted in_review card rejects approve and reject.
    service.delete_task(active_todo.id)
    with pytest.raises(InvalidStateTransitionError):
        service.approve_task(active_todo.id)
    with pytest.raises(InvalidStateTransitionError):
        service.reject_task(active_todo.id, "不应允许审核已删卡")
    assert service.get_task_detail(active_todo.id).task.status == TaskStatus.IN_REVIEW
    assert service.get_task_detail(active_todo.id).reviews == []


def test_sqlite_mutex_between_concurrent_delete_and_start(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "mutex.db")

    for i in range(20):
        task = store.create_task(f"互斥测试-{i}", "desc", str(tmp_path), "mock-dev")
        barrier = threading.Barrier(2)
        outcomes: dict[str, object] = {}

        def do_delete() -> None:
            barrier.wait()
            try:
                outcomes["delete"] = store.delete_task(task.id)
            except Exception as exc:  # noqa: BLE001
                outcomes["delete"] = exc

        def do_start() -> None:
            barrier.wait()
            try:
                outcomes["start"] = store.claim_task_for_run(
                    task.id, "mock-dev", "mock", "mock-v1", tmp_path / "logs"
                )
            except Exception as exc:  # noqa: BLE001
                outcomes["start"] = exc

        t1 = threading.Thread(target=do_delete)
        t2 = threading.Thread(target=do_start)
        t1.start()
        t2.start()
        t1.join(timeout=10.0)
        t2.join(timeout=10.0)

        del_ok = not isinstance(outcomes["delete"], Exception)
        start_ok = not isinstance(outcomes["start"], Exception)
        assert del_ok != start_ok, f"Iteration {i}: expected exactly one winner, got {outcomes}"

        final = store.get_task(task.id)
        runs = store.list_runs(task.id)
        if del_ok:
            assert isinstance(outcomes["start"], InvalidStateTransitionError)
            assert final.is_deleted is True
            assert final.status == TaskStatus.TODO
            assert runs == []
        else:
            assert isinstance(outcomes["delete"], CardAlreadyRunningError)
            assert final.is_deleted is False
            assert final.status == TaskStatus.IN_PROGRESS
            assert len(runs) == 1 and runs[0].status == RunStatus.RUNNING


def test_legacy_db_concurrent_migration_adds_deleted_at_losslessly(tmp_path: Path) -> None:
    db_path = tmp_path / "old_schema.db"
    conn = sqlite3.connect(str(db_path))
    conn.executescript(
        """
        CREATE TABLE sequences (name TEXT PRIMARY KEY, value INTEGER NOT NULL);
        INSERT INTO sequences(name, value) VALUES ('task', 1);
        CREATE TABLE tasks (
            id TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            description TEXT NOT NULL,
            status TEXT NOT NULL,
            assigned_agent TEXT,
            repo_path TEXT NOT NULL,
            worktree_path TEXT,
            worktree_branch TEXT,
            last_run_id TEXT,
            last_error TEXT,
            last_review_comment TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        INSERT INTO tasks (
            id, title, description, status, assigned_agent, repo_path, created_at, updated_at
        ) VALUES (
            'task-001', '历史旧卡', '旧描述', 'in_review', 'mock-dev', '/tmp',
            '2026-11-23T00:00:00+00:00', '2026-11-23T00:00:00+00:00'
        );
        """
    )
    conn.commit()
    conn.close()

    barrier = threading.Barrier(10)

    def open_and_read() -> tuple[str, bool]:
        barrier.wait()
        store = SQLiteStore(db_path)
        t = store.get_task("task-001")
        return t.id, t.is_deleted

    with ThreadPoolExecutor(max_workers=10) as pool:
        results = [f.result() for f in [pool.submit(open_and_read) for _ in range(10)]]

    assert results == [("task-001", False)] * 10
    store = SQLiteStore(db_path)
    migrated = store.get_task("task-001")
    data = migrated.to_dict()
    for legacy_key in (
        "id",
        "title",
        "description",
        "status",
        "status_zh",
        "assigned_agent",
        "repo_path",
        "worktree_path",
        "worktree_branch",
        "last_run_id",
        "last_error",
        "last_review_comment",
        "created_at",
        "updated_at",
    ):
        assert legacy_key in data
    assert data["deleted"] is False
    assert data["deleted_at"] is None

    # Migrated legacy card supports delete & restore preserving status.
    store.delete_task("task-001")
    assert store.list_tasks() == []
    assert [t.id for t in store.list_tasks(deleted=True)] == ["task-001"]
    restored = store.restore_task("task-001")
    assert restored.status == TaskStatus.IN_REVIEW
    assert restored.assigned_agent == "mock-dev"


def test_workbench_delete_recycle_bin_restore_and_guards(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    service = CrewlineService(repo / ".crewline")
    service.init_workspace()

    t1 = service.create_task("卡片一", "[mock:file=one.md]", repo, "mock-dev")
    t2 = service.create_task("卡片二", "[mock:file=two.md]", repo, "mock-qa")
    assert service.run_tasks([t1.id])[0].succeeded is True

    wb = wb_module.Workbench(service)
    monkeypatch.setattr(wb_module, "_terminal", lambda: _NullCtx())

    # 1. `x` shows card ID in confirmation prompt; declining cancels delete.
    seen_prompts: list[str] = []

    def decline(label: str) -> str:
        seen_prompts.append(label)
        return "n"

    monkeypatch.setattr(wb, "_prompt", decline)
    wb.handle("x", service.get_task_detail(t1.id).task)
    assert any(t1.id in p for p in seen_prompts)
    assert service.get_task_detail(t1.id).task.is_deleted is False

    # 2. Confirming `x` deletes t1; active draw hides t1 and shows `x 删除` & `b 回收区`.
    monkeypatch.setattr(wb, "_prompt", lambda _label: "y")
    wb.handle("x", service.get_task_detail(t1.id).task)
    assert service.get_task_detail(t1.id).task.is_deleted is True

    screen = _FakeScreen()
    monkeypatch.setattr(wb_module.curses, "has_colors", lambda: False)
    assert t1.id in wb.message and "回收区" in wb.message
    wb.message = ""
    selected_active = wb.draw(screen)  # type: ignore[arg-type]
    active_rendered = "\n".join(screen.lines)
    assert selected_active is not None and selected_active.id == t2.id
    assert t1.id not in active_rendered
    assert t1.title not in active_rendered
    assert "x 删除" in active_rendered
    assert "b 回收区" in active_rendered

    # 3. `b` switches to recycle bin: shows t1, recycle help, allows logs/diff, blocks start/assign/review.
    wb.handle("b", selected_active)
    assert wb.show_deleted is True
    selected_deleted = wb.draw(screen)  # type: ignore[arg-type]
    deleted_rendered = "\n".join(screen.lines)
    assert selected_deleted is not None and selected_deleted.id == t1.id
    assert "u 恢复" in deleted_rendered
    assert "回收区（只读，不可启动）" in deleted_rendered

    # Logs and diff work read-only in recycle bin.
    paged: list[str] = []
    monkeypatch.setattr(wb_module.click, "echo_via_pager", lambda text: paged.append(text))
    wb.handle("l", selected_deleted)
    wb.handle("d", selected_deleted)
    assert len(paged) == 2
    assert "one.md" in paged[0] and "one.md" in paged[1]

    # Starting/assigning/reviewing in recycle bin is safely blocked without touching sessions.
    start_called = False

    def fail_if_called(_task_id: str) -> object:
        nonlocal start_called
        start_called = True
        raise AssertionError("Should not start deleted task in recycle bin")

    monkeypatch.setattr(wb.sessions, "start", fail_if_called)
    for blocked_key in ("s", "\n", "a", "p", "r", "x", "n"):
        wb.handle(blocked_key, selected_deleted)
        assert "回收区" in wb.message
    assert start_called is False

    # 4. `u` restores t1 with original status & assignment, then `b` returns to active board.
    wb.handle("u", selected_deleted)
    restored = service.get_task_detail(t1.id).task
    assert restored.is_deleted is False
    assert restored.status == TaskStatus.IN_REVIEW
    assert restored.assigned_agent == "mock-dev"

    wb.handle("b", None)
    assert wb.show_deleted is False
    selected_back = wb.draw(screen)  # type: ignore[arg-type]
    assert selected_back is not None
    assert {t.id for t in service.list_tasks()} == {t1.id, t2.id}
