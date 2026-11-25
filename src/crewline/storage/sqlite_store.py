"""SQLite persistence with atomic task claiming and run history."""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, Iterator

from crewline.domain.errors import (
    CardAlreadyRunningError,
    InvalidStateTransitionError,
    TaskNotFoundError,
)
from crewline.domain.models import (
    ReviewDecision,
    ReviewRecord,
    RunRecord,
    RunStatus,
    TaskCard,
    TaskStatus,
    utc_now_iso,
)
from crewline.domain.state_machine import ensure_transition

SCHEMA_STATEMENTS: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS sequences (
        name TEXT PRIMARY KEY,
        value INTEGER NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS tasks (
        id TEXT PRIMARY KEY,
        title TEXT NOT NULL,
        description TEXT NOT NULL,
        status TEXT NOT NULL,
        assigned_agent TEXT,
        repo_path TEXT NOT NULL,
        worktree_path TEXT,
        worktree_branch TEXT,
        worktree_base_commit TEXT,
        last_run_id TEXT,
        last_error TEXT,
        last_review_comment TEXT,
        deleted_at TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS runs (
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
    )
    """,
    """
    CREATE UNIQUE INDEX IF NOT EXISTS idx_runs_one_active_per_task
        ON runs(task_id)
        WHERE status = 'running'
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_runs_task_started
        ON runs(task_id, started_at DESC)
    """,
    """
    CREATE TABLE IF NOT EXISTS workbench_runs (
        run_id TEXT PRIMARY KEY REFERENCES runs(id) ON DELETE CASCADE,
        launch_id TEXT NOT NULL UNIQUE
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS reviews (
        id TEXT PRIMARY KEY,
        task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
        run_id TEXT REFERENCES runs(id),
        decision TEXT NOT NULL,
        comment TEXT NOT NULL,
        created_at TEXT NOT NULL
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_reviews_task_created
        ON reviews(task_id, created_at DESC)
    """,
)


class SQLiteStore:
    """Thread-safe and multi-process-safe SQLite store for Crewline state."""

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(
            str(self.db_path),
            timeout=10.0,
            isolation_level=None,  # explicit BEGIN IMMEDIATE transactions
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=10000")
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.execute("COMMIT")
        except Exception:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()

    def _init_schema(self) -> None:
        with self._transaction() as conn:
            for statement in SCHEMA_STATEMENTS:
                conn.execute(statement)
            columns = {
                row["name"]
                for row in conn.execute("PRAGMA table_info(tasks)").fetchall()
            }
            if "worktree_base_commit" not in columns:
                conn.execute("ALTER TABLE tasks ADD COLUMN worktree_base_commit TEXT")
            if "deleted_at" not in columns:
                conn.execute("ALTER TABLE tasks ADD COLUMN deleted_at TEXT")

    def _next_id(self, conn: sqlite3.Connection, seq_name: str, prefix: str, width: int) -> str:
        conn.execute(
            "INSERT INTO sequences(name, value) VALUES (?, 0) ON CONFLICT(name) DO NOTHING",
            (seq_name,),
        )
        conn.execute(
            "UPDATE sequences SET value = value + 1 WHERE name = ?",
            (seq_name,),
        )
        row = conn.execute(
            "SELECT value FROM sequences WHERE name = ?",
            (seq_name,),
        ).fetchone()
        return f"{prefix}-{int(row['value']):0{width}d}"

    def _row_to_task(self, row: sqlite3.Row) -> TaskCard:
        return TaskCard(
            id=row["id"],
            title=row["title"],
            description=row["description"],
            status=TaskStatus(row["status"]),
            assigned_agent=row["assigned_agent"],
            repo_path=row["repo_path"],
            worktree_path=row["worktree_path"],
            worktree_branch=row["worktree_branch"],
            worktree_base_commit=row["worktree_base_commit"],
            last_run_id=row["last_run_id"],
            last_error=row["last_error"],
            last_review_comment=row["last_review_comment"],
            deleted_at=row["deleted_at"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def _row_to_run(self, row: sqlite3.Row) -> RunRecord:
        return RunRecord(
            id=row["id"],
            task_id=row["task_id"],
            agent_name=row["agent_name"],
            provider=row["provider"],
            model=row["model"],
            status=RunStatus(row["status"]),
            started_at=row["started_at"],
            finished_at=row["finished_at"],
            exit_code=row["exit_code"],
            worktree_path=row["worktree_path"],
            worktree_branch=row["worktree_branch"],
            log_path=row["log_path"],
            summary=row["summary"],
            error_message=row["error_message"],
            changed_files=json.loads(row["changed_files_json"] or "[]"),
        )

    def _row_to_review(self, row: sqlite3.Row) -> ReviewRecord:
        return ReviewRecord(
            id=row["id"],
            task_id=row["task_id"],
            run_id=row["run_id"],
            decision=ReviewDecision(row["decision"]),
            comment=row["comment"],
            created_at=row["created_at"],
        )

    def create_task(
        self,
        title: str,
        description: str,
        repo_path: str,
        assigned_agent: str | None = None,
    ) -> TaskCard:
        now = utc_now_iso()
        with self._transaction() as conn:
            task_id = self._next_id(conn, "task", "task", 3)
            conn.execute(
                """
                INSERT INTO tasks (
                    id, title, description, status, assigned_agent,
                    repo_path, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    task_id,
                    title,
                    description,
                    TaskStatus.TODO.value,
                    assigned_agent,
                    repo_path,
                    now,
                    now,
                ),
            )
            row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
            return self._row_to_task(row)

    def get_task(self, task_id: str) -> TaskCard:
        conn = self._connect()
        try:
            row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
            if row is None:
                raise TaskNotFoundError(f"未找到任务卡 '{task_id}'。")
            return self._row_to_task(row)
        finally:
            conn.close()

    def list_tasks(
        self,
        status: TaskStatus | None = None,
        *,
        deleted: bool = False,
    ) -> list[TaskCard]:
        conn = self._connect()
        try:
            deleted_clause = "deleted_at IS NOT NULL" if deleted else "deleted_at IS NULL"
            if status is None:
                rows = conn.execute(
                    f"SELECT * FROM tasks WHERE {deleted_clause} ORDER BY id ASC"
                ).fetchall()
            else:
                rows = conn.execute(
                    f"SELECT * FROM tasks WHERE {deleted_clause} AND status = ? ORDER BY id ASC",
                    (status.value,),
                ).fetchall()
            return [self._row_to_task(row) for row in rows]
        finally:
            conn.close()

    def delete_task(self, task_id: str) -> TaskCard:
        """Soft-delete a task card inside BEGIN IMMEDIATE, rejecting active runs."""
        now = utc_now_iso()
        with self._transaction() as conn:
            row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
            if row is None:
                raise TaskNotFoundError(f"未找到任务卡 '{task_id}'。")
            task = self._row_to_task(row)
            if task.is_deleted:
                raise InvalidStateTransitionError(f"任务卡 {task_id} 已在回收区中。")
            if task.status == TaskStatus.IN_PROGRESS:
                raise CardAlreadyRunningError(
                    f"任务卡 {task_id} 正在处理中，无法删除。"
                )
            active_run = conn.execute(
                "SELECT id FROM runs WHERE task_id = ? AND status = ?",
                (task_id, RunStatus.RUNNING.value),
            ).fetchone()
            if active_run is not None:
                raise CardAlreadyRunningError(
                    f"任务卡 {task_id} 存在活动运行 ({active_run['id']})，无法删除。"
                )
            conn.execute(
                "UPDATE tasks SET deleted_at = ?, updated_at = ? WHERE id = ?",
                (now, now, task_id),
            )
            updated = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
            return self._row_to_task(updated)

    def restore_task(self, task_id: str) -> TaskCard:
        """Restore a soft-deleted task card keeping its original ID, status, and assignment."""
        now = utc_now_iso()
        with self._transaction() as conn:
            row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
            if row is None:
                raise TaskNotFoundError(f"未找到任务卡 '{task_id}'。")
            task = self._row_to_task(row)
            if not task.is_deleted:
                raise InvalidStateTransitionError(f"任务卡 {task_id} 未被删除，无需恢复。")
            conn.execute(
                "UPDATE tasks SET deleted_at = NULL, updated_at = ? WHERE id = ?",
                (now, task_id),
            )
            updated = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
            return self._row_to_task(updated)

    def assign_task(self, task_id: str, agent_name: str) -> TaskCard:
        now = utc_now_iso()
        with self._transaction() as conn:
            row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
            if row is None:
                raise TaskNotFoundError(f"未找到任务卡 '{task_id}'。")
            task = self._row_to_task(row)
            if task.is_deleted:
                raise InvalidStateTransitionError(
                    f"任务卡 {task_id} 已删除，无法分派；请先恢复任务卡。"
                )
            if task.status == TaskStatus.IN_PROGRESS:
                raise CardAlreadyRunningError(
                    f"任务卡 {task_id} 正在处理中，无法在运行期间重新分派。"
                )
            if task.status == TaskStatus.DONE:
                raise InvalidStateTransitionError(
                    f"任务卡 {task_id} 已审核完成，无法重新分派。"
                )
            conn.execute(
                "UPDATE tasks SET assigned_agent = ?, updated_at = ? WHERE id = ?",
                (agent_name, now, task_id),
            )
            updated = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
            return self._row_to_task(updated)

    def claim_task_for_run(
        self,
        task_id: str,
        agent_name: str,
        provider: str,
        model: str,
        log_dir: Path,
        *,
        launch_id: str | None = None,
    ) -> tuple[TaskCard, RunRecord]:
        """Atomically transition todo -> in_progress and create a running RunRecord.

        Prevents duplicate concurrent starts and delete/start races via BEGIN IMMEDIATE.
        """
        now = utc_now_iso()
        with self._transaction() as conn:
            row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
            if row is None:
                raise TaskNotFoundError(f"未找到任务卡 '{task_id}'。")
            task = self._row_to_task(row)
            if task.is_deleted:
                raise InvalidStateTransitionError(
                    f"任务卡 {task_id} 已删除，无法启动；请先恢复任务卡。"
                )
            ensure_transition(task.id, task.status, TaskStatus.IN_PROGRESS)

            active_run = conn.execute(
                "SELECT id FROM runs WHERE task_id = ? AND status = ?",
                (task_id, RunStatus.RUNNING.value),
            ).fetchone()
            if active_run is not None:
                raise CardAlreadyRunningError(
                    f"任务卡 {task_id} 已有运行中的记录 ({active_run['id']})，不能重复启动。"
                )

            run_id = self._next_id(conn, "run", "run", 4)
            log_path = str((log_dir / f"{run_id}.log").resolve())

            try:
                conn.execute(
                    """
                    INSERT INTO runs (
                        id, task_id, agent_name, provider, model, status,
                        started_at, worktree_path, worktree_branch, log_path
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        run_id,
                        task_id,
                        agent_name,
                        provider,
                        model,
                        RunStatus.RUNNING.value,
                        now,
                        task.worktree_path,
                        task.worktree_branch,
                        log_path,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise CardAlreadyRunningError(
                    f"任务卡 {task_id} 正在运行中，不能重复启动。"
                ) from exc
            if launch_id is not None:
                conn.execute(
                    "INSERT INTO workbench_runs (run_id, launch_id) VALUES (?, ?)",
                    (run_id, launch_id),
                )

            conn.execute(
                """
                UPDATE tasks
                SET status = ?, last_run_id = ?, updated_at = ?
                WHERE id = ?
                """,
                (TaskStatus.IN_PROGRESS.value, run_id, now, task_id),
            )
            updated_task = self._row_to_task(
                conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
            )
            created_run = self._row_to_run(
                conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
            )
            return updated_task, created_run

    def update_run_workspace(
        self,
        task_id: str,
        run_id: str,
        worktree_path: str | None,
        worktree_branch: str | None,
        worktree_base_commit: str | None = None,
    ) -> None:
        now = utc_now_iso()
        with self._transaction() as conn:
            conn.execute(
                "UPDATE runs SET worktree_path = ?, worktree_branch = ? WHERE id = ?",
                (worktree_path, worktree_branch, run_id),
            )
            conn.execute(
                """
                UPDATE tasks
                SET worktree_path = ?,
                    worktree_branch = ?,
                    worktree_base_commit = COALESCE(worktree_base_commit, ?),
                    updated_at = ?
                WHERE id = ?
                """,
                (worktree_path, worktree_branch, worktree_base_commit, now, task_id),
            )

    def finish_run_success(
        self,
        task_id: str,
        run_id: str,
        exit_code: int,
        summary: str,
        changed_files: list[str],
        worktree_path: str | None,
        worktree_branch: str | None,
        worktree_base_commit: str | None = None,
    ) -> tuple[TaskCard, RunRecord]:
        now = utc_now_iso()
        with self._transaction() as conn:
            row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
            if row is None:
                raise TaskNotFoundError(f"未找到任务卡 '{task_id}'。")
            task = self._row_to_task(row)
            if task.last_run_id != run_id:
                raise InvalidStateTransitionError(f"运行 {run_id} 已被其他运行替换。")
            ensure_transition(task.id, task.status, TaskStatus.IN_REVIEW)

            conn.execute(
                """
                UPDATE runs
                SET status = ?, finished_at = ?, exit_code = ?,
                    summary = ?, error_message = NULL,
                    changed_files_json = ?, worktree_path = ?, worktree_branch = ?
                WHERE id = ?
                """,
                (
                    RunStatus.SUCCEEDED.value,
                    now,
                    exit_code,
                    summary,
                    json.dumps(changed_files, ensure_ascii=False),
                    worktree_path,
                    worktree_branch,
                    run_id,
                ),
            )
            conn.execute(
                """
                UPDATE tasks
                SET status = ?, last_error = NULL,
                    worktree_path = COALESCE(?, worktree_path),
                    worktree_branch = COALESCE(?, worktree_branch),
                    worktree_base_commit = COALESCE(worktree_base_commit, ?),
                    updated_at = ?
                WHERE id = ?
                """,
                (
                    TaskStatus.IN_REVIEW.value,
                    worktree_path,
                    worktree_branch,
                    worktree_base_commit,
                    now,
                    task_id,
                ),
            )
            updated_task = self._row_to_task(
                conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
            )
            updated_run = self._row_to_run(
                conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
            )
            return updated_task, updated_run

    def finish_run_failure(
        self,
        task_id: str,
        run_id: str,
        exit_code: int,
        error_message: str,
        summary: str | None,
        changed_files: list[str],
        worktree_path: str | None,
        worktree_branch: str | None,
        worktree_base_commit: str | None = None,
    ) -> tuple[TaskCard, RunRecord]:
        now = utc_now_iso()
        with self._transaction() as conn:
            row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
            if row is None:
                raise TaskNotFoundError(f"未找到任务卡 '{task_id}'。")
            task = self._row_to_task(row)
            if task.last_run_id != run_id:
                raise InvalidStateTransitionError(f"运行 {run_id} 已被其他运行替换。")
            ensure_transition(task.id, task.status, TaskStatus.TODO)

            conn.execute(
                """
                UPDATE runs
                SET status = ?, finished_at = ?, exit_code = ?,
                    summary = ?, error_message = ?,
                    changed_files_json = ?, worktree_path = ?, worktree_branch = ?
                WHERE id = ?
                """,
                (
                    RunStatus.FAILED.value,
                    now,
                    exit_code,
                    summary,
                    error_message,
                    json.dumps(changed_files, ensure_ascii=False),
                    worktree_path,
                    worktree_branch,
                    run_id,
                ),
            )
            conn.execute(
                """
                UPDATE tasks
                SET status = ?, last_error = ?,
                    worktree_path = COALESCE(?, worktree_path),
                    worktree_branch = COALESCE(?, worktree_branch),
                    worktree_base_commit = COALESCE(worktree_base_commit, ?),
                    updated_at = ?
                WHERE id = ?
                """,
                (
                    TaskStatus.TODO.value,
                    error_message,
                    worktree_path,
                    worktree_branch,
                    worktree_base_commit,
                    now,
                    task_id,
                ),
            )
            updated_task = self._row_to_task(
                conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
            )
            updated_run = self._row_to_run(
                conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
            )
            return updated_task, updated_run

    def record_review(
        self,
        task_id: str,
        decision: ReviewDecision,
        comment: str,
        *,
        clear_worktree_path: bool = False,
        before_commit: Callable[[TaskCard], None] | None = None,
    ) -> tuple[TaskCard, ReviewRecord]:
        now = utc_now_iso()
        target_status = (
            TaskStatus.DONE if decision == ReviewDecision.APPROVED else TaskStatus.TODO
        )
        with self._transaction() as conn:
            row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
            if row is None:
                raise TaskNotFoundError(f"未找到任务卡 '{task_id}'。")
            task = self._row_to_task(row)
            if task.is_deleted:
                raise InvalidStateTransitionError(
                    f"任务卡 {task_id} 已删除，无法审核；请先恢复任务卡。"
                )
            if task.status != TaskStatus.IN_REVIEW:
                raise InvalidStateTransitionError(f"任务卡 {task_id} 当前为「{task.status.label_zh}」，仅待审核时可审核。")
            ensure_transition(task.id, task.status, target_status)

            should_clear_worktree = False
            if before_commit is not None:
                before_commit(task)
                should_clear_worktree = clear_worktree_path and bool(task.worktree_path)
            else:
                should_clear_worktree = clear_worktree_path

            review_id = self._next_id(conn, "review", "rev", 4)
            conn.execute(
                """
                INSERT INTO reviews (id, task_id, run_id, decision, comment, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    review_id,
                    task_id,
                    task.last_run_id,
                    decision.value,
                    comment,
                    now,
                ),
            )
            if should_clear_worktree:
                conn.execute(
                    """
                    UPDATE tasks
                    SET status = ?, last_review_comment = ?, worktree_path = NULL, updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        target_status.value,
                        comment,
                        now,
                        task_id,
                    ),
                )
            else:
                conn.execute(
                    """
                    UPDATE tasks
                    SET status = ?, last_review_comment = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        target_status.value,
                        comment,
                        now,
                        task_id,
                    ),
                )
            updated_task = self._row_to_task(
                conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
            )
            created_review = self._row_to_review(
                conn.execute("SELECT * FROM reviews WHERE id = ?", (review_id,)).fetchone()
            )
            return updated_task, created_review

    def clear_worktree_info(self, task_id: str) -> None:
        now = utc_now_iso()
        with self._transaction() as conn:
            conn.execute(
                """
                UPDATE tasks
                SET worktree_path = NULL, updated_at = ?
                WHERE id = ?
                """,
                (now, task_id),
            )

    def list_runs(self, task_id: str) -> list[RunRecord]:
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT * FROM runs WHERE task_id = ? ORDER BY id ASC",
                (task_id,),
            ).fetchall()
            return [self._row_to_run(row) for row in rows]
        finally:
            conn.close()

    def list_active_workbench_runs(self) -> list[RunRecord]:
        conn = self._connect()
        try:
            rows = conn.execute(
                """
                SELECT runs.* FROM runs JOIN workbench_runs ON workbench_runs.run_id = runs.id
                WHERE runs.status = ?
                """,
                (RunStatus.RUNNING.value,),
            ).fetchall()
            return [self._row_to_run(row) for row in rows]
        finally:
            conn.close()

    def get_workbench_run(self, launch_id: str) -> RunRecord | None:
        conn = self._connect()
        try:
            row = conn.execute(
                """
                SELECT runs.* FROM runs JOIN workbench_runs ON workbench_runs.run_id = runs.id
                WHERE workbench_runs.launch_id = ?
                """,
                (launch_id,),
            ).fetchone()
            return self._row_to_run(row) if row is not None else None
        finally:
            conn.close()

    def get_run(self, run_id: str) -> RunRecord | None:
        conn = self._connect()
        try:
            row = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
            return self._row_to_run(row) if row is not None else None
        finally:
            conn.close()

    def list_reviews(self, task_id: str) -> list[ReviewRecord]:
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT * FROM reviews WHERE task_id = ? ORDER BY id ASC",
                (task_id,),
            ).fetchall()
            return [self._row_to_review(row) for row in rows]
        finally:
            conn.close()
