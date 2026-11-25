"""Focused tests for Git review reliability, base commits, legacy DBs, and safe worktree cleanup."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import threading

from click.testing import CliRunner
import pytest

from crewline.cli import workbench as wb_module
from crewline.cli.main import cli
from crewline.domain.errors import InvalidStateTransitionError, WorkspaceError
from crewline.domain.models import TaskCard, TaskStatus
from crewline.engine.service import CrewlineService
from crewline.storage.sqlite_store import SQLiteStore
from crewline.workspace.git_worktree import MAX_UNTRACKED_FILE_BYTES


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


def _init_git_repo(repo_dir: Path) -> str:
    repo_dir.mkdir(parents=True, exist_ok=True)
    _git(repo_dir, "init")
    _git(repo_dir, "config", "user.name", "Crewline Review Test")
    _git(repo_dir, "config", "user.email", "review@crewline.local")
    (repo_dir / ".gitignore").write_text(".crewline/\n", encoding="utf-8")
    (repo_dir / "app.txt").write_text("line1\n", encoding="utf-8")
    (repo_dir / "space file.txt").write_text("orig space\n", encoding="utf-8")
    (repo_dir / "中文文件.txt").write_text("原始内容\n", encoding="utf-8")
    _git(repo_dir, "add", ".")
    _git(repo_dir, "commit", "-m", "initial commit")
    return _git(repo_dir, "rev-parse", "HEAD")


def test_base_commit_persists_across_retry_reconnect_and_main_repo_changes(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    initial_sha = _init_git_repo(repo)
    service = CrewlineService(repo / ".crewline")
    service.init_workspace()

    task = service.create_task(
        title="基准提交稳定性测试",
        description="[mock:file=deliverable.md] 首次交付",
        repo_path=repo,
        assigned_agent="mock-dev",
    )
    assert service.run_tasks([task.id])[0].succeeded is True

    detail = service.get_task_detail(task.id).task
    assert detail.worktree_base_commit == initial_sha
    wt = Path(detail.worktree_path or "")

    # Commit work inside the task worktree.
    _git(wt, "add", "deliverable.md")
    _git(wt, "commit", "-m", "first task commit")

    # Main repo switches to another branch and adds new commits.
    _git(repo, "checkout", "-b", "feature/unrelated")
    (repo / "unrelated.txt").write_text("main repo moved\n", encoding="utf-8")
    _git(repo, "add", "unrelated.txt")
    _git(repo, "commit", "-m", "main repo unrelated commit")
    new_main_sha = _git(repo, "rev-parse", "HEAD")
    assert new_main_sha != initial_sha

    # Reject and retry on existing worktree -> base commit must remain initial_sha.
    service.reject_task(task.id, "请补充第二轮交付")
    assert service.run_tasks([task.id])[0].succeeded is True
    detail_after_retry = service.get_task_detail(task.id).task
    assert detail_after_retry.worktree_base_commit == initial_sha

    # Remove clean worktree externally and reconnect existing branch on another retry -> base still preserved.
    _git(wt, "add", "deliverable.md")
    _git(wt, "commit", "-m", "second task commit")
    _git(repo, "worktree", "remove", str(wt))
    assert not wt.exists()

    service.reject_task(task.id, "重连已有分支再试一次")
    assert service.run_tasks([task.id])[0].succeeded is True
    detail_after_reconnect = service.get_task_detail(task.id).task
    assert detail_after_reconnect.worktree_base_commit == initial_sha

    diff_text = service.get_diff(task.id)
    assert initial_sha[:12] in diff_text
    assert "first task commit" in diff_text
    assert "second task commit" in diff_text
    assert "unrelated.txt" not in diff_text


def test_diff_shows_committed_staged_unstaged_untracked_and_matches_workbench(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    home = repo / ".crewline"
    service = CrewlineService(home)
    service.init_workspace()

    task = service.create_task(
        title="完整变更视图测试",
        description="[mock:file=init_note.md] 初始化工作区",
        repo_path=repo,
        assigned_agent="mock-dev",
    )
    assert service.run_tasks([task.id])[0].succeeded is True
    detail = service.get_task_detail(task.id).task
    wt = Path(detail.worktree_path or "")

    # 1. Committed changes (normal + space + Chinese paths)
    (wt / "committed note.md").write_text("committed with space\n", encoding="utf-8")
    (wt / "app.txt").write_text("line1\ncommitted change\n", encoding="utf-8")
    (wt / "中文文件.txt").write_text("原始内容\n已提交中文改动\n", encoding="utf-8")
    _git(wt, "add", "init_note.md", "committed note.md", "app.txt", "中文文件.txt")
    _git(wt, "commit", "-m", "commit deliverable with unicode path")

    # Even when working tree is completely clean, committed changes since base must be visible!
    clean_diff = service.get_diff(task.id)
    assert "已提交变更" in clean_diff
    assert "committed note.md" in clean_diff
    assert "已提交中文改动" in clean_diff
    assert "无文件变更" not in clean_diff

    # 2. Staged changes (modified space path + newly added Chinese path + file overwritten in worktree)
    (wt / "space file.txt").write_text("orig space\nstaged space edit\n", encoding="utf-8")
    (wt / "已暂存 文档.txt").write_text("暂存的新内容\n", encoding="utf-8")
    (wt / "app.txt").write_text("line1\ncommitted change\nstaged v1 content\n", encoding="utf-8")
    _git(wt, "add", "space file.txt", "已暂存 文档.txt", "app.txt")

    # 3. Unstaged changes (overwriting the same file that has staged v1 content)
    (wt / "app.txt").write_text("line1\ncommitted change\nunstaged v2 overwrite\n", encoding="utf-8")

    # 4. Untracked files (normal + space + Chinese)
    (wt / "untracked plain.txt").write_text("untracked plain body\n", encoding="utf-8")
    (wt / "未跟踪 报告.md").write_text("中文未跟踪内容\n", encoding="utf-8")

    # Verify CLI `crewline diff` and Workbench `d` key share the exact same output.
    runner = CliRunner()
    cli_res = runner.invoke(cli, ["--home", str(home), "diff", task.id])
    assert cli_res.exit_code == 0, cli_res.output

    wb = wb_module.Workbench(service)
    paged: list[str] = []
    monkeypatch.setattr(wb_module, "_terminal", lambda: _null_ctx())
    monkeypatch.setattr(wb_module.click, "echo_via_pager", lambda text: paged.append(text))
    wb.handle("d", service.get_task_detail(task.id).task)

    assert len(paged) == 1
    assert cli_res.output.rstrip("\n") == paged[0].rstrip("\n")

    diff_text = paged[0]
    # Committed
    assert "已提交变更" in diff_text
    assert "commit deliverable with unicode path" in diff_text
    assert "已提交中文改动" in diff_text
    # Staged
    assert "已暂存变更" in diff_text
    assert "staged space edit" in diff_text
    assert "staged v1 content" in diff_text
    assert "已暂存 文档.txt" in diff_text
    assert "暂存的新内容" in diff_text
    # Unstaged (same file overwritten after staging)
    assert "未暂存变更" in diff_text
    assert "unstaged v2 overwrite" in diff_text
    # Untracked
    assert "新增未跟踪文件" in diff_text
    assert "untracked plain.txt" in diff_text
    assert "untracked plain body" in diff_text
    assert "未跟踪 报告.md" in diff_text
    assert "中文未跟踪内容" in diff_text


def test_untracked_size_limit_symlink_protection_and_git_error_visibility(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    outside_secret = tmp_path / "outside_secret.txt"
    outside_secret.write_text("TOP_SECRET_OUTSIDE_CONTENT_DO_NOT_READ\n", encoding="utf-8")

    outside_dir = tmp_path / "outside_dir"
    outside_dir.mkdir()
    (outside_dir / "nested.txt").write_text("OUTSIDE_DIR_SECRET\n", encoding="utf-8")

    service = CrewlineService(repo / ".crewline")
    service.init_workspace()
    task = service.create_task("安全与大文件预览测试", "[mock:file=ok.txt]", repo, "mock-dev")
    assert service.run_tasks([task.id])[0].succeeded is True
    wt = Path(service.get_task_detail(task.id).task.worktree_path or "")

    # Large untracked file exceeding per-file limit
    large_file = wt / "large_output.txt"
    large_file.write_text("A" * (MAX_UNTRACKED_FILE_BYTES + 2048), encoding="utf-8")

    # Binary untracked file
    (wt / "sample.bin").write_bytes(b"\x00\x01\x02binary-payload")

    # Symlinks: outside file, outside directory, and self-referencing loop (loop -> loop)
    os.symlink(str(outside_secret), str(wt / "escape_link.txt"))
    os.symlink(str(outside_dir), str(wt / "escape_dir"))
    os.symlink("loop", str(wt / "loop"))

    diff_text = service.get_diff(task.id)
    assert "已截断" in diff_text
    assert "二进制文件" in diff_text
    assert "TOP_SECRET_OUTSIDE_CONTENT_DO_NOT_READ" not in diff_text
    assert "OUTSIDE_DIR_SECRET" not in diff_text
    assert "escape_link.txt" in diff_text
    assert f"符号链接 -> {outside_secret}" in diff_text
    assert "符号链接 -> loop" in diff_text

    # Corrupt .git inside worktree -> must raise WorkspaceError, NOT report "no changes".
    git_pointer = wt / ".git"
    backup_git = git_pointer.read_text(encoding="utf-8")
    git_pointer.write_text("gitdir: /nonexistent/broken/path\n", encoding="utf-8")
    try:
        with pytest.raises(WorkspaceError):
            service.get_diff(task.id)
    finally:
        git_pointer.write_text(backup_git, encoding="utf-8")

    # Removed .git inside nested worktree must not walk up to main repo .git (diff or cleanup).
    git_pointer.unlink()
    with pytest.raises(WorkspaceError):
        service.get_diff(task.id)
    with pytest.raises(WorkspaceError):
        service.approve_task(task.id, cleanup_worktree=True)
    assert service.get_task_detail(task.id).task.status == TaskStatus.IN_REVIEW


def test_legacy_sqlite_schema_and_missing_base_commit_compatibility(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    home = repo / ".crewline"
    home.mkdir(parents=True, exist_ok=True)

    # Create a real worktree manually as if created by an older version of Crewline.
    wt = home / "worktrees" / "task-001"
    wt.parent.mkdir(parents=True, exist_ok=True)
    _git(repo, "worktree", "add", "-b", "crewline/task-001", str(wt), "HEAD")

    (wt / "app.txt").write_text("line1\nold commit change\n", encoding="utf-8")
    _git(wt, "add", "app.txt")
    _git(wt, "commit", "-m", "legacy task commit")
    (wt / "staged_legacy.txt").write_text("legacy staged\n", encoding="utf-8")
    _git(wt, "add", "staged_legacy.txt")
    (wt / "app.txt").write_text("line1\nold commit change\nlegacy unstaged\n", encoding="utf-8")
    (wt / "untracked_legacy.txt").write_text("legacy untracked\n", encoding="utf-8")

    # Build a legacy SQLite DB WITHOUT worktree_base_commit column.
    db_path = home / "crewline.db"
    conn = sqlite3.connect(str(db_path))
    conn.executescript(
        """
        CREATE TABLE sequences (name TEXT PRIMARY KEY, value INTEGER NOT NULL);
        INSERT INTO sequences(name, value) VALUES ('task', 1), ('run', 1);
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
        CREATE TABLE runs (
            id TEXT PRIMARY KEY,
            task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
            agent_name TEXT NOT NULL,
            provider TEXT NOT NULL,
            model TEXT NOT NULL,
            status TEXT NOT NULL,
            started_at TEXT NOT NULL,
            finished_at TEXT,
            exit_code INTEGER,
            worktree_path TEXT,
            worktree_branch TEXT,
            log_path TEXT NOT NULL,
            summary TEXT,
            error_message TEXT,
            changed_files_json TEXT NOT NULL DEFAULT '[]'
        );
        CREATE TABLE reviews (
            id TEXT PRIMARY KEY,
            task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
            run_id TEXT REFERENCES runs(id),
            decision TEXT NOT NULL,
            comment TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        """
    )
    conn.execute(
        """
        INSERT INTO tasks (
            id, title, description, status, assigned_agent, repo_path,
            worktree_path, worktree_branch, last_run_id, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "task-001",
            "旧版数据库任务卡",
            "旧任务描述",
            "in_review",
            "mock-dev",
            str(repo),
            str(wt),
            "crewline/task-001",
            "run-0001",
            "2026-11-23T00:00:00+00:00",
            "2026-11-23T00:00:00+00:00",
        ),
    )
    conn.execute(
        """
        INSERT INTO runs (
            id, task_id, agent_name, provider, model, status, started_at,
            finished_at, exit_code, worktree_path, worktree_branch, log_path, summary
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "run-0001",
            "task-001",
            "mock-dev",
            "mock",
            "mock-v1",
            "succeeded",
            "2026-11-23T00:00:00+00:00",
            "2026-11-23T00:01:00+00:00",
            0,
            str(wt),
            "crewline/task-001",
            str(home / "logs" / "run-0001.log"),
            "legacy ok",
        ),
    )
    conn.commit()
    conn.close()

    # Initialize config only (keep legacy DB intact) and verify CLI/JSON + diff fallback.
    service = CrewlineService(home)
    service.init_workspace(force=False)

    runner = CliRunner()
    res_json = runner.invoke(cli, ["--home", str(home), "task", "show", "task-001", "--json"])
    assert res_json.exit_code == 0, res_json.output
    payload = json.loads(res_json.output)
    assert payload["task"]["id"] == "task-001"
    assert payload["task"]["worktree_base_commit"] is None

    res_diff = runner.invoke(cli, ["--home", str(home), "diff", "task-001"])
    assert res_diff.exit_code == 0, res_diff.output
    assert "缺少可靠的基准提交记录" in res_diff.output
    assert "legacy staged" in res_diff.output
    assert "legacy unstaged" in res_diff.output
    assert "legacy untracked" in res_diff.output

    # Unreachable recorded base commit SHA should also warn clearly and show uncommitted changes.
    conn2 = sqlite3.connect(str(db_path))
    try:
        conn2.execute(
            "UPDATE tasks SET worktree_base_commit = ? WHERE id = ?",
            ("deadbeef" * 5, "task-001"),
        )
        conn2.commit()
    finally:
        conn2.close()

    res_unreachable = runner.invoke(cli, ["--home", str(home), "diff", "task-001"])
    assert res_unreachable.exit_code == 0, res_unreachable.output
    assert "不可达" in res_unreachable.output
    assert "legacy staged" in res_unreachable.output
    assert "legacy unstaged" in res_unreachable.output
    assert "legacy untracked" in res_unreachable.output


def test_cleanup_refuses_dirty_worktree_allows_normal_approve_and_cleans_clean_worktree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    home = repo / ".crewline"
    service = CrewlineService(home)
    service.init_workspace()
    runner = CliRunner()

    # Card 1: dirty untracked -> refuse cleanup -> still can approve normally (retaining worktree).
    t1 = service.create_task("脏工作区保护-普通通过", "[mock:file=draft.md]", repo, "mock-dev")
    assert service.run_tasks([t1.id])[0].succeeded is True
    wt1 = Path(service.get_task_detail(t1.id).task.worktree_path or "")

    res_dirty = runner.invoke(
        cli, ["--home", str(home), "review", "approve", t1.id, "--cleanup-worktree"]
    )
    assert res_dirty.exit_code == 1
    assert "拒绝清理脏 worktree" in res_dirty.output
    # Everything preserved: file, branch, worktree_path, and in_review status.
    t1_after = service.get_task_detail(t1.id)
    assert t1_after.task.status == TaskStatus.IN_REVIEW
    assert t1_after.task.worktree_path == str(wt1)
    assert (wt1 / "draft.md").exists()
    assert not t1_after.reviews
    assert _git(repo, "branch", "--list", f"crewline/{t1.id}")

    # User chooses normal approve (without --cleanup-worktree) -> succeeds and preserves worktree.
    res_plain = runner.invoke(
        cli, ["--home", str(home), "review", "approve", t1.id, "--comment", "保留工作区通过"]
    )
    assert res_plain.exit_code == 0, res_plain.output
    t1_done = service.get_task_detail(t1.id).task
    assert t1_done.status == TaskStatus.DONE
    assert t1_done.worktree_path == str(wt1)
    assert (wt1 / "draft.md").exists()

    # Card 2: dirty staged/unstaged refused -> commit -> clean cleanup succeeds & preserves result branch.
    t2 = service.create_task("脏工作区保护-提交后清理", "[mock:file=final.md]", repo, "mock-dev")
    assert service.run_tasks([t2.id])[0].succeeded is True
    wt2 = Path(service.get_task_detail(t2.id).task.worktree_path or "")

    # Stage file -> still dirty -> refused.
    _git(wt2, "add", "final.md")
    with pytest.raises(WorkspaceError):
        service.approve_task(t2.id, cleanup_worktree=True)
    assert service.get_task_detail(t2.id).task.status == TaskStatus.IN_REVIEW

    # Commit file -> clean worktree -> but lock worktree first to verify Git errors are visible!
    _git(wt2, "commit", "-m", "deliver final.md")
    _git(repo, "worktree", "lock", str(wt2))
    with pytest.raises(WorkspaceError, match="Git 清理 worktree 失败"):
        service.approve_task(t2.id, cleanup_worktree=True)
    assert service.get_task_detail(t2.id).task.status == TaskStatus.IN_REVIEW
    assert wt2.exists()

    # Unlock clean worktree -> cleanup succeeds, directory removed, result branch preserved with commit.
    _git(repo, "worktree", "unlock", str(wt2))
    approved_t2, _ = service.approve_task(t2.id, comment="干净清理通过", cleanup_worktree=True)
    assert approved_t2.status == TaskStatus.DONE
    assert approved_t2.worktree_path is None
    assert approved_t2.worktree_branch == f"crewline/{t2.id}"
    assert not wt2.exists()
    assert _git(repo, "branch", "--list", f"crewline/{t2.id}")
    assert "final.md" in _git(repo, "ls-tree", "-r", "--name-only", f"crewline/{t2.id}")

    # Card 3: Workbench ordinary approve (`p`) preserves worktree.
    t3 = service.create_task("工作台普通审核保留 worktree", "[mock:file=wb.md]", repo, "mock-dev")
    assert service.run_tasks([t3.id])[0].succeeded is True
    wt3 = Path(service.get_task_detail(t3.id).task.worktree_path or "")

    wb = wb_module.Workbench(service)
    monkeypatch.setattr(wb, "_prompt", lambda _label: "y")
    wb.handle("p", service.get_task_detail(t3.id).task)

    t3_done = service.get_task_detail(t3.id).task
    assert t3_done.status == TaskStatus.DONE
    assert t3_done.worktree_path == str(wt3)
    assert (wt3 / "wb.md").exists()


def test_cleanup_refuses_ignored_local_files_and_cleans_after_removal(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    (repo / ".gitignore").write_text(".crewline/\n*.local\nignored_cache/\n", encoding="utf-8")
    _git(repo, "add", ".gitignore")
    _git(repo, "commit", "-m", "add ignore rules")

    service = CrewlineService(repo / ".crewline")
    service.init_workspace()
    task = service.create_task("忽略文件保护测试", "[mock:file=deliver.md]", repo, "mock-dev")
    assert service.run_tasks([task.id])[0].succeeded is True
    wt = Path(service.get_task_detail(task.id).task.worktree_path or "")

    # Commit tracked deliverable so tracked/untracked status is clean, then add ignored local files.
    _git(wt, "add", "deliver.md")
    _git(wt, "commit", "-m", "commit deliverable")
    ignored_file = wt / "secret.local"
    ignored_file.write_text("local ignored artifact\n", encoding="utf-8")
    ignored_dir = wt / "ignored_cache"
    ignored_dir.mkdir()
    (ignored_dir / "dump.bin").write_text("cached output\n", encoding="utf-8")

    # Non-force git worktree remove would delete ignored files; Crewline must refuse cleanup.
    with pytest.raises(WorkspaceError, match="拒绝清理脏 worktree"):
        service.approve_task(task.id, cleanup_worktree=True)

    still_review = service.get_task_detail(task.id).task
    assert still_review.status == TaskStatus.IN_REVIEW
    assert still_review.worktree_path == str(wt)
    assert ignored_file.exists()
    assert (ignored_dir / "dump.bin").exists()

    # Once ignored files are removed, clean worktree cleanup succeeds and keeps result branch.
    ignored_file.unlink()
    (ignored_dir / "dump.bin").unlink()
    ignored_dir.rmdir()

    approved, _ = service.approve_task(task.id, comment="清理通过", cleanup_worktree=True)
    assert approved.status == TaskStatus.DONE
    assert approved.worktree_path is None
    assert not wt.exists()
    assert _git(repo, "branch", "--list", f"crewline/{task.id}")


def test_concurrent_legacy_db_migration_no_duplicate_column(tmp_path: Path) -> None:
    db_path = tmp_path / "legacy.db"
    conn = sqlite3.connect(str(db_path))
    conn.executescript(
        """
        CREATE TABLE sequences (name TEXT PRIMARY KEY, value INTEGER NOT NULL);
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
            'task-001', '旧任务', 'desc', 'todo', 'mock-dev', '/tmp',
            '2026-11-23T00:00:00+00:00', '2026-11-23T00:00:00+00:00'
        );
        """
    )
    conn.commit()
    conn.close()

    barrier = threading.Barrier(10)

    def open_store() -> str:
        barrier.wait()
        store = SQLiteStore(db_path)
        return store.get_task("task-001").id

    with ThreadPoolExecutor(max_workers=10) as pool:
        results = [f.result() for f in [pool.submit(open_store) for _ in range(10)]]

    assert results == ["task-001"] * 10
    verify_store = SQLiteStore(db_path)
    assert verify_store.get_task("task-001").worktree_base_commit is None


def test_concurrent_approve_serialized_winner_decides_worktree_fate(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    home = repo / ".crewline"
    setup_service = CrewlineService(home)
    setup_service.init_workspace()

    # Case A: Service A (cleanup_worktree=True) enters cleanup first and holds the serialized review tx
    # while Service B (plain approve) races concurrently -> A wins, directory removed, worktree_path=None.
    t_a = setup_service.create_task("并发审核-清理先赢", "[mock:file=a.md]", repo, "mock-dev")
    assert setup_service.run_tasks([t_a.id])[0].succeeded is True
    wt_a = Path(setup_service.get_task_detail(t_a.id).task.worktree_path or "")
    _git(wt_a, "add", "a.md")
    _git(wt_a, "commit", "-m", "commit a")

    service_a = CrewlineService(home)
    service_b = CrewlineService(home)
    cleanup_entered = threading.Event()
    release_cleanup = threading.Event()
    orig_cleanup_a = service_a.workspace_backend.cleanup

    def paused_cleanup(task: TaskCard) -> None:
        cleanup_entered.set()
        assert release_cleanup.wait(timeout=5.0)
        orig_cleanup_a(task)

    service_a.workspace_backend.cleanup = paused_cleanup  # type: ignore[method-assign]

    res_a: list[object] = []
    res_b: list[object] = []

    def run_a() -> None:
        try:
            res_a.append(service_a.approve_task(t_a.id, comment="A清理通过", cleanup_worktree=True))
        except Exception as exc:  # noqa: BLE001
            res_a.append(exc)

    def run_b() -> None:
        assert cleanup_entered.wait(timeout=5.0)
        try:
            res_b.append(service_b.approve_task(t_a.id, comment="B普通通过", cleanup_worktree=False))
        except Exception as exc:  # noqa: BLE001
            res_b.append(exc)

    thread_a = threading.Thread(target=run_a)
    thread_b = threading.Thread(target=run_b)
    thread_a.start()
    thread_b.start()
    assert cleanup_entered.wait(timeout=5.0)
    # Give B a moment to enter BEGIN IMMEDIATE wait before releasing A.
    threading.Event().wait(0.15)
    release_cleanup.set()
    thread_a.join(timeout=10.0)
    thread_b.join(timeout=10.0)

    assert len(res_a) == 1 and not isinstance(res_a[0], Exception)
    assert len(res_b) == 1 and isinstance(res_b[0], InvalidStateTransitionError)
    final_a = setup_service.get_task_detail(t_a.id)
    assert final_a.task.status == TaskStatus.DONE
    assert final_a.task.worktree_path is None
    assert not wt_a.exists()
    assert len(final_a.reviews) == 1

    # Case B: Service B (plain approve) finishes first while Service A (cleanup_worktree=True) races ->
    # B wins, A NEVER calls cleanup, worktree directory & worktree_path remain intact.
    t_b = setup_service.create_task("并发审核-普通通过先赢", "[mock:file=b.md]", repo, "mock-dev")
    assert setup_service.run_tasks([t_b.id])[0].succeeded is True
    wt_b = Path(setup_service.get_task_detail(t_b.id).task.worktree_path or "")
    _git(wt_b, "add", "b.md")
    _git(wt_b, "commit", "-m", "commit b")

    service_a2 = CrewlineService(home)
    service_b2 = CrewlineService(home)
    cleanup_called = threading.Event()
    orig_cleanup_a2 = service_a2.workspace_backend.cleanup

    def tracked_cleanup(task: TaskCard) -> None:
        cleanup_called.set()
        orig_cleanup_a2(task)

    service_a2.workspace_backend.cleanup = tracked_cleanup  # type: ignore[method-assign]

    # B approves first (plain approve), then A attempts cleanup approve.
    done_b, _ = service_b2.approve_task(t_b.id, comment="B先通过", cleanup_worktree=False)
    assert done_b.status == TaskStatus.DONE
    with pytest.raises(InvalidStateTransitionError):
        service_a2.approve_task(t_b.id, comment="A后清理", cleanup_worktree=True)

    assert not cleanup_called.is_set()
    final_b = setup_service.get_task_detail(t_b.id).task
    assert final_b.status == TaskStatus.DONE
    assert final_b.worktree_path == str(wt_b)
    assert wt_b.exists()

    # Case C: Dirty worktree causes A's cleanup to fail & roll back; concurrent plain approve B wins and keeps files.
    t_c = setup_service.create_task("并发审核-清理拒绝后普通通过", "[mock:file=c.md]", repo, "mock-dev")
    assert setup_service.run_tasks([t_c.id])[0].succeeded is True
    wt_c = Path(setup_service.get_task_detail(t_c.id).task.worktree_path or "")

    service_a3 = CrewlineService(home)
    service_b3 = CrewlineService(home)
    dirty_entered = threading.Event()
    release_dirty = threading.Event()
    orig_cleanup_a3 = service_a3.workspace_backend.cleanup

    def paused_dirty_cleanup(task: TaskCard) -> None:
        dirty_entered.set()
        assert release_dirty.wait(timeout=5.0)
        orig_cleanup_a3(task)

    service_a3.workspace_backend.cleanup = paused_dirty_cleanup  # type: ignore[method-assign]

    res_a3: list[object] = []
    res_b3: list[object] = []

    def run_a3() -> None:
        try:
            res_a3.append(service_a3.approve_task(t_c.id, cleanup_worktree=True))
        except Exception as exc:  # noqa: BLE001
            res_a3.append(exc)

    def run_b3() -> None:
        assert dirty_entered.wait(timeout=5.0)
        try:
            res_b3.append(service_b3.approve_task(t_c.id, comment="B保留脏目录通过", cleanup_worktree=False))
        except Exception as exc:  # noqa: BLE001
            res_b3.append(exc)

    ta3 = threading.Thread(target=run_a3)
    tb3 = threading.Thread(target=run_b3)
    ta3.start()
    tb3.start()
    assert dirty_entered.wait(timeout=5.0)
    threading.Event().wait(0.15)
    release_dirty.set()
    ta3.join(timeout=10.0)
    tb3.join(timeout=10.0)

    assert len(res_a3) == 1 and isinstance(res_a3[0], WorkspaceError)
    assert len(res_b3) == 1 and not isinstance(res_b3[0], Exception)
    final_c = setup_service.get_task_detail(t_c.id).task
    assert final_c.status == TaskStatus.DONE
    assert final_c.worktree_path == str(wt_c)
    assert (wt_c / "c.md").exists()


class _null_ctx:
    def __enter__(self) -> None:
        return None

    def __exit__(self, *args: object) -> None:
        return None
