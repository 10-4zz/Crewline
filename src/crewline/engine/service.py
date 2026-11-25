"""Crewline application service decoupled from CLI for reuse by UI, SSH, and Lead Agents."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from crewline.adapters.base import ExecutionRequest, build_task_prompt
from crewline.adapters.interactive import execute_interactive
from crewline.adapters.registry import AdapterRegistry, create_default_registry
from crewline.config.store import (
    CONFIG_FILENAME,
    PROVIDER_ALLOWED_AUTH,
    PROVIDER_DEFAULT_ENV,
    PROVIDER_DEFAULT_MODEL,
    CrewlineConfig,
    default_agents,
    load_config,
    save_config,
    validate_agent_config,
)
from crewline.domain.errors import (
    AgentNotFoundError,
    ConfigError,
    CrewlineError,
    InvalidStateTransitionError,
    WorkspaceError,
)
from crewline.domain.models import (
    AgentConfig,
    AuthMethod,
    DoctorItem,
    ProviderId,
    ReviewDecision,
    ReviewRecord,
    RunRecord,
    TaskCard,
    TaskStatus,
    utc_now_iso,
)
from crewline.security.redactor import SecretRedactor
from crewline.storage.sqlite_store import SQLiteStore
from crewline.workspace.git_worktree import LocalGitWorktreeManager, WorkspaceBackend


@dataclass(slots=True)
class TaskDetail:
    """Complete view of a task card with its run and review history."""

    task: TaskCard
    runs: list[RunRecord]
    reviews: list[ReviewRecord]


@dataclass(slots=True)
class RunOutcome:
    """Outcome of attempting to execute a task card."""

    task_id: str
    task: TaskCard | None
    run: RunRecord | None
    started: bool
    succeeded: bool
    error: str | None = None


class CrewlineService:
    """Core orchestration service used by the CLI and extensible to GUI/SSH/Lead Agents."""

    def __init__(
        self,
        home_dir: Path,
        registry: AdapterRegistry | None = None,
        workspace_backend: WorkspaceBackend | None = None,
    ) -> None:
        self.home_dir = home_dir.resolve()
        self.config_path = self.home_dir / CONFIG_FILENAME
        self.db_path = self.home_dir / "crewline.db"
        self.logs_dir = self.home_dir / "logs"
        self.registry = registry or create_default_registry()
        self.workspace_backend: WorkspaceBackend = (
            workspace_backend or LocalGitWorktreeManager(self.home_dir)
        )

    def _store(self) -> SQLiteStore:
        return SQLiteStore(self.db_path)

    def _config(self) -> CrewlineConfig:
        return load_config(self.config_path)

    def init_workspace(self, force: bool = False) -> tuple[Path, bool]:
        """Initialize local .crewline directory, SQLite DB, and starter config.toml."""
        self.home_dir.mkdir(parents=True, exist_ok=True)
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        (self.home_dir / "worktrees").mkdir(parents=True, exist_ok=True)

        created_config = False
        if force or not self.config_path.exists():
            cfg = CrewlineConfig(agents=default_agents())
            save_config(self.config_path, cfg)
            created_config = True

        # Initialize SQLite schema immediately.
        self._store()
        return self.home_dir, created_config

    # --- Agent configuration & diagnostics ---

    def list_agents(self) -> list[AgentConfig]:
        cfg = self._config()
        return list(cfg.agents.values())

    def get_agent(self, name: str) -> AgentConfig:
        cfg = self._config()
        agent = cfg.agents.get(name)
        if agent is None:
            raise AgentNotFoundError(
                f"未找到名为 '{name}' 的 Agent。请运行 `crewline agent list` 查看已配置的 Agent。"
            )
        return agent

    def add_agent(
        self,
        name: str,
        provider: ProviderId,
        model: str | None = None,
        auth_method: AuthMethod | None = None,
        api_key_env: str | None = None,
        cli_bin: str | None = None,
        base_url: str | None = None,
        extra_args: list[str] | None = None,
        description: str = "",
        overwrite: bool = False,
    ) -> AgentConfig:
        cfg = self._config()
        if name in cfg.agents and not overwrite:
            raise ConfigError(
                f"Agent '{name}' 已存在。如需覆盖请传入 --overwrite，或先移除原配置。"
            )
        resolved_auth = auth_method or next(iter(PROVIDER_ALLOWED_AUTH[provider]))
        resolved_model = (
            model if model is not None else PROVIDER_DEFAULT_MODEL.get(provider, "")
        )
        resolved_env = api_key_env
        if resolved_auth == AuthMethod.API_KEY and not resolved_env:
            resolved_env = PROVIDER_DEFAULT_ENV.get(provider)

        agent = validate_agent_config(
            AgentConfig(
                name=name,
                provider=provider,
                model=resolved_model,
                auth_method=resolved_auth,
                api_key_env=resolved_env,
                cli_bin=cli_bin,
                base_url=base_url,
                extra_args=list(extra_args or []),
                description=description,
            )
        )
        cfg.agents[name] = agent
        save_config(self.config_path, cfg)
        return agent

    def remove_agent(self, name: str) -> None:
        cfg = self._config()
        if name not in cfg.agents:
            raise AgentNotFoundError(f"未找到名为 '{name}' 的 Agent。")
        del cfg.agents[name]
        save_config(self.config_path, cfg)

    def doctor(self, agent_name: str | None = None) -> list[DoctorItem]:
        cfg = self._config()
        if agent_name is not None:
            agent = self.get_agent(agent_name)
            adapter = self.registry.get(agent.provider)
            return [adapter.check_health(agent)]

        report: list[DoctorItem] = []
        for agent in cfg.agents.values():
            adapter = self.registry.get(agent.provider)
            report.append(adapter.check_health(agent))
        return report

    # --- Task management ---

    def create_task(
        self,
        title: str,
        description: str = "",
        repo_path: Path | None = None,
        assigned_agent: str | None = None,
    ) -> TaskCard:
        title_clean = title.strip()
        if not title_clean:
            raise ConfigError("任务卡标题不能为空。")
        if assigned_agent is not None:
            self.get_agent(assigned_agent)
        resolved_repo = str((repo_path or Path.cwd()).resolve())
        return self._store().create_task(
            title=title_clean,
            description=description,
            repo_path=resolved_repo,
            assigned_agent=assigned_agent,
        )

    def list_tasks(
        self,
        status: TaskStatus | None = None,
        *,
        deleted: bool = False,
    ) -> list[TaskCard]:
        return self._store().list_tasks(status=status, deleted=deleted)

    def get_task_detail(self, task_id: str) -> TaskDetail:
        store = self._store()
        task = store.get_task(task_id)
        runs = store.list_runs(task_id)
        reviews = store.list_reviews(task_id)
        return TaskDetail(task=task, runs=runs, reviews=reviews)

    def assign_task(self, task_id: str, agent_name: str) -> TaskCard:
        self.get_agent(agent_name)
        return self._store().assign_task(task_id=task_id, agent_name=agent_name)

    def delete_task(self, task_id: str) -> TaskCard:
        """Soft-delete a task card into the recycle bin while keeping history and worktree."""
        return self._store().delete_task(task_id=task_id)

    def restore_task(self, task_id: str) -> TaskCard:
        """Restore a soft-deleted task card with its original ID, status, and assignment."""
        return self._store().restore_task(task_id=task_id)

    # --- Parallel task execution ---

    def run_tasks(
        self,
        task_ids: Sequence[str],
        max_workers: int = 4,
        timeout_seconds: int | None = None,
    ) -> list[RunOutcome]:
        if not task_ids:
            raise ConfigError("请指定至少一个任务卡 ID（或使用 --all-todo）。")

        worker_count = max(1, min(max_workers, len(task_ids)))
        outcomes: list[RunOutcome | None] = [None] * len(task_ids)

        if worker_count == 1:
            for idx, task_id in enumerate(task_ids):
                outcomes[idx] = self._run_single_task(task_id, timeout_seconds)
        else:
            with ThreadPoolExecutor(max_workers=worker_count) as pool:
                future_to_idx = {
                    pool.submit(self._run_single_task, task_id, timeout_seconds): idx
                    for idx, task_id in enumerate(task_ids)
                }
                for future in as_completed(future_to_idx):
                    idx = future_to_idx[future]
                    outcomes[idx] = future.result()

        return [item for item in outcomes if item is not None]

    def run_all_todo(
        self,
        max_workers: int = 4,
        timeout_seconds: int | None = None,
    ) -> list[RunOutcome]:
        todo_tasks = [
            t for t in self.list_tasks(status=TaskStatus.TODO) if t.assigned_agent
        ]
        if not todo_tasks:
            raise ConfigError("当前没有已分派 Agent 的待办任务卡。")
        return self.run_tasks(
            [t.id for t in todo_tasks],
            max_workers=max_workers,
            timeout_seconds=timeout_seconds,
        )

    def _run_single_task(
        self,
        task_id: str,
        timeout_seconds: int | None,
        *,
        launch_id: str | None = None,
    ) -> RunOutcome:
        store = self._store()
        try:
            cfg = self._config()
            task = store.get_task(task_id)
            if task.is_deleted:
                raise InvalidStateTransitionError(
                    f"任务卡 {task_id} 已删除，无法启动；请先恢复任务卡。"
                )
            if not task.assigned_agent:
                raise ConfigError(
                    f"任务卡 {task_id} 尚未分派 Agent，请先执行 `crewline task assign {task_id} <agent>`。"
                )
            agent = self.get_agent(task.assigned_agent)
            adapter = self.registry.get(agent.provider)
            prior_runs_count = len(store.list_runs(task_id))

            self.logs_dir.mkdir(parents=True, exist_ok=True)
            claimed_task, run = store.claim_task_for_run(
                task_id=task_id,
                agent_name=agent.name,
                provider=agent.provider.value,
                model=agent.model or adapter.spec.default_model or "(CLI 默认)",
                log_dir=self.logs_dir,
                launch_id=launch_id,
            )
        except CrewlineError as exc:
            return RunOutcome(
                task_id=task_id,
                task=None,
                run=None,
                started=False,
                succeeded=False,
                error=str(exc),
            )

        redactor = SecretRedactor(env_var_names=cfg.env_var_refs())
        log_path = Path(run.log_path)

        def append_log(message: str) -> None:
            cleaned = redactor.redact_str(message)
            timestamp = utc_now_iso()
            with log_path.open("a", encoding="utf-8") as fh:
                fh.write(f"[{timestamp}] {cleaned}\n")
            if launch_id is not None and agent.auth_method != AuthMethod.OFFICIAL_CLI:
                print(cleaned, flush=True)

        effective_timeout = (
            timeout_seconds if timeout_seconds is not None else cfg.default_timeout_seconds
        )
        worktree_path_str: str | None = claimed_task.worktree_path
        worktree_branch: str | None = claimed_task.worktree_branch
        worktree_base_commit: str | None = claimed_task.worktree_base_commit

        workspace = None
        try:
            append_log(
                f"=== Crewline Run {run.id} | Task {claimed_task.id}: {claimed_task.title} ==="
            )
            append_log(
                f"Agent={agent.name} Provider={agent.provider.value} Model={run.model} Auth={agent.auth_method.value}"
            )
            workspace = self.workspace_backend.prepare(claimed_task)
            if workspace.is_git_worktree:
                worktree_path_str = str(workspace.cwd)
                worktree_branch = workspace.branch
                worktree_base_commit = worktree_base_commit or workspace.base_commit
                store.update_run_workspace(
                    claimed_task.id,
                    run.id,
                    worktree_path_str,
                    worktree_branch,
                    worktree_base_commit=worktree_base_commit,
                )
                base_note = (
                    f"，基准提交: {worktree_base_commit[:12]}"
                    if worktree_base_commit
                    else ""
                )
                append_log(
                    f"已准备独立 Git worktree: {worktree_path_str} (分支: {worktree_branch}{base_note})"
                )
            else:
                append_log(f"目标目录非 Git 仓库，将在目录直接运行: {workspace.cwd}")

            prompt = build_task_prompt(
                task=claimed_task,
                is_git_worktree=workspace.is_git_worktree,
                capabilities=adapter.spec.capabilities,
            )
            request = ExecutionRequest(
                task=claimed_task,
                run_id=run.id,
                agent=agent,
                cwd=workspace.cwd,
                prompt=prompt,
                timeout_seconds=effective_timeout,
                log_callback=append_log,
                redactor=redactor,
                is_git_worktree=workspace.is_git_worktree,
                worktree_branch=worktree_branch,
                prior_runs_count=prior_runs_count,
            )
            if launch_id is not None and agent.auth_method == AuthMethod.OFFICIAL_CLI:
                result = execute_interactive(request, adapter.spec.default_cli_bin)
            else:
                result = adapter.execute(request)
            changes = self.workspace_backend.summarize_changes(workspace)
            if changes.changed_files:
                append_log(f"工作区变更文件: {', '.join(changes.changed_files)}")

            if result.success:
                safe_summary = redactor.redact_str(result.summary or "执行成功")
                append_log(f"=== Run {run.id} SUCCEEDED -> 状态流转为 待审核(in_review) ===")
                updated_task, updated_run = store.finish_run_success(
                    task_id=claimed_task.id,
                    run_id=run.id,
                    exit_code=result.exit_code,
                    summary=safe_summary,
                    changed_files=changes.changed_files,
                    worktree_path=worktree_path_str,
                    worktree_branch=worktree_branch,
                    worktree_base_commit=worktree_base_commit,
                )
                return RunOutcome(
                    task_id=claimed_task.id,
                    task=updated_task,
                    run=updated_run,
                    started=True,
                    succeeded=True,
                )

            safe_error = redactor.redact_str(
                result.error_message or result.summary or f"执行失败（exit_code={result.exit_code}）"
            )
            safe_summary = redactor.redact_str(result.summary or "执行失败")
            append_log(
                f"=== Run {run.id} FAILED (exit_code={result.exit_code}) -> 状态退回 待办(todo) ==="
            )
            append_log(f"失败原因: {safe_error}")
            updated_task, updated_run = store.finish_run_failure(
                task_id=claimed_task.id,
                run_id=run.id,
                exit_code=result.exit_code,
                error_message=safe_error,
                summary=safe_summary,
                changed_files=changes.changed_files,
                worktree_path=worktree_path_str,
                worktree_branch=worktree_branch,
                worktree_base_commit=worktree_base_commit,
            )
            return RunOutcome(
                task_id=claimed_task.id,
                task=updated_task,
                run=updated_run,
                started=True,
                succeeded=False,
                error=safe_error,
            )
        except Exception as exc:  # noqa: BLE001
            safe_error = redactor.redact_str(str(exc))
            try:
                append_log(f"运行过程发生异常: {safe_error}")
                append_log(f"=== Run {run.id} FAILED -> 状态退回 待办(todo) ===")
            except OSError:
                pass
            changed_files: list[str] = []
            if workspace is not None:
                try:
                    changed_files = self.workspace_backend.summarize_changes(
                        workspace
                    ).changed_files
                except Exception:  # noqa: BLE001
                    changed_files = []
            updated_task, updated_run = store.finish_run_failure(
                task_id=claimed_task.id,
                run_id=run.id,
                exit_code=1,
                error_message=safe_error,
                summary="执行异常中断",
                changed_files=changed_files,
                worktree_path=worktree_path_str,
                worktree_branch=worktree_branch,
                worktree_base_commit=worktree_base_commit,
            )
            return RunOutcome(
                task_id=claimed_task.id,
                task=updated_task,
                run=updated_run,
                started=True,
                succeeded=False,
                error=safe_error,
            )

    def run_workbench_task(self, task_id: str, launch_id: str) -> RunOutcome:
        return self._run_single_task(task_id, timeout_seconds=None, launch_id=launch_id)

    def list_active_workbench_runs(self) -> list[RunRecord]:
        return self._store().list_active_workbench_runs()

    def get_workbench_run(self, launch_id: str) -> RunRecord | None:
        return self._store().get_workbench_run(launch_id)

    def recover_workbench_run(self, run: RunRecord) -> bool:
        try:
            self._store().finish_run_failure(
                task_id=run.task_id,
                run_id=run.id,
                exit_code=1,
                error_message="后台会话已中断，可重新启动任务卡。",
                summary="后台会话已中断",
                changed_files=run.changed_files,
                worktree_path=run.worktree_path,
                worktree_branch=run.worktree_branch,
            )
        except InvalidStateTransitionError:
            return False
        return True

    # --- Logs, diffs, and human review ---

    def read_logs(
        self,
        task_id: str,
        run_id: str | None = None,
        tail: int | None = None,
    ) -> tuple[RunRecord, str]:
        store = self._store()
        task = store.get_task(task_id)
        runs = store.list_runs(task_id)
        if not runs:
            raise ConfigError(f"任务卡 {task_id} 还没有任何运行记录。")

        target_run: RunRecord | None = None
        if run_id is not None:
            for item in runs:
                if item.id == run_id:
                    target_run = item
                    break
            if target_run is None:
                raise ConfigError(f"任务卡 {task_id} 不存在运行记录 '{run_id}'。")
        else:
            last_id = task.last_run_id
            for item in reversed(runs):
                if item.id == last_id:
                    target_run = item
                    break
            target_run = target_run or runs[-1]

        log_path = Path(target_run.log_path)
        if not log_path.exists():
            return target_run, "（日志文件尚未写入或已被清理）"
        content = log_path.read_text(encoding="utf-8", errors="replace")
        if tail is not None and tail > 0:
            lines = content.splitlines()
            content = "\n".join(lines[-tail:])
        return target_run, content

    def get_diff(self, task_id: str) -> str:
        task = self._store().get_task(task_id)
        if not task.worktree_path:
            raise WorkspaceError(
                f"任务卡 {task_id} 当前没有关联的 Git worktree（可能尚未运行，或目标目录非 Git 仓库）。"
            )
        return self.workspace_backend.get_diff(
            Path(task.worktree_path),
            base_commit=task.worktree_base_commit,
            branch=task.worktree_branch,
        )

    def approve_task(
        self,
        task_id: str,
        comment: str = "",
        cleanup_worktree: bool = False,
    ) -> tuple[TaskCard, ReviewRecord]:
        store = self._store()

        def _cleanup_inside_review_tx(locked_task: TaskCard) -> None:
            if locked_task.worktree_path:
                self.workspace_backend.cleanup(locked_task)

        return store.record_review(
            task_id=task_id,
            decision=ReviewDecision.APPROVED,
            comment=comment.strip() or "审核通过",
            clear_worktree_path=cleanup_worktree,
            before_commit=_cleanup_inside_review_tx if cleanup_worktree else None,
        )

    def reject_task(
        self,
        task_id: str,
        comment: str,
    ) -> tuple[TaskCard, ReviewRecord]:
        cleaned = comment.strip()
        if not cleaned:
            raise ConfigError("退回待办时必须附带审核意见（--comment）。")
        return self._store().record_review(
            task_id=task_id,
            decision=ReviewDecision.REJECTED,
            comment=cleaned,
        )
