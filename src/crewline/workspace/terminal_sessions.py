"""Detached workbench sessions using tmux-owned pseudo-terminals."""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from uuid import uuid4

from crewline.adapters.cli_common import find_cli_binary
from crewline.config.store import PROVIDER_DEFAULT_ENV, load_config
from crewline.domain.errors import ConfigError, InvalidStateTransitionError, WorkspaceError
from crewline.domain.models import AuthMethod, ProviderId, RunRecord, TaskStatus
from crewline.engine.service import CrewlineService
from crewline.runtime import clean_subprocess_env, crewline_self_command, crewline_worker_env

_CLI_CREDENTIALS = {
    ProviderId.CODEX: {"OPENAI_API_KEY", "CODEX_API_KEY", "CODEX_ACCESS_TOKEN"},
    ProviderId.CLAUDE_CODE: {"ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN"},
    ProviderId.GROK_BUILD: {"XAI_API_KEY"},
}


class TerminalSessions:
    def __init__(self, service: CrewlineService) -> None:
        self.service = service
        self.tmux = shutil.which("tmux")
        self.namespace = hashlib.sha256(os.fsencode(service.home_dir)).hexdigest()[:16]

    def _command(self, task_id: str, *args: str) -> list[str]:
        if not self.tmux:
            raise ConfigError("启动和连接会话需要安装 tmux。")
        # Per-task server snapshots the selected Agent's environment on launch.
        return [self.tmux, "-f", "/dev/null", "-L", f"crewline-{self.namespace}-{task_id}", *args]

    def _environment(
        self,
        provider: ProviderId | None = None,
        key_ref: str | None = None,
        *,
        for_worker: bool = False,
    ) -> dict[str, str]:
        credentials = set(PROVIDER_DEFAULT_ENV.values())
        for names in _CLI_CREDENTIALS.values():
            credentials.update(names)
        if self.service.config_path.is_file():
            credentials.update(load_config(self.service.config_path).env_var_refs())
        allowed = _CLI_CREDENTIALS.get(provider, set()) if provider is not None else set()
        if key_ref:
            allowed = allowed | {key_ref}
        excluded = (credentials - allowed) | {"TMUX", "TMUX_PANE"}
        base_env = crewline_worker_env() if for_worker else clean_subprocess_env()
        return {
            key: value
            for key, value in base_env.items()
            if key not in excluded
        }

    @staticmethod
    def _call(command: list[str], env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
        try:
            return subprocess.run(
                command,
                capture_output=True,
                text=True,
                env=clean_subprocess_env() if env is None else env,
                timeout=3,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise WorkspaceError(f"无法连接 tmux 会话：{exc}") from exc

    def start(self, task_id: str) -> RunRecord:
        task = self.service.get_task_detail(task_id).task
        if task.is_deleted:
            raise InvalidStateTransitionError(
                f"任务卡 {task_id} 已删除，无法启动会话；请先恢复任务卡。"
            )
        if task.status != TaskStatus.TODO or not task.assigned_agent:
            raise ConfigError("请选择已指派 Agent 的待办任务。")
        agent = self.service.get_agent(task.assigned_agent)
        adapter = self.service.registry.get(agent.provider)
        if agent.auth_method == AuthMethod.OFFICIAL_CLI:
            binary = agent.cli_bin or adapter.spec.default_cli_bin
            if not binary or not find_cli_binary(binary):
                raise ConfigError(f"找不到官方 CLI {binary}，请先安装并登录。")
        if agent.api_key_env and not os.environ.get(agent.api_key_env, "").strip():
            raise ConfigError(f"请在启动 Crewline 前配置环境变量 {agent.api_key_env}。")

        launch_id = uuid4().hex
        command = self._command(
            task.id,
            "new-session",
            "-d",
            "-E",
            "-s",
            "work",
            "-c",
            str(Path.cwd()),
            *crewline_self_command(),
            "--home",
            str(self.service.home_dir),
            "_workbench-run",
            task.id,
            launch_id,
        )
        result = self._call(
            command,
            self._environment(agent.provider, agent.api_key_env, for_worker=True),
        )
        if result.returncode != 0:
            raise WorkspaceError(f"无法启动后台会话：{result.stderr.strip()}")
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            run = self.service.get_workbench_run(launch_id)
            if run is not None:
                return run
            if self.is_alive(task.id) is False:
                run = self.service.get_workbench_run(launch_id)
                if run is not None:
                    return run
                break
            time.sleep(0.05)
        raise WorkspaceError(f"未能确认 {task.id} 已启动。请查看任务状态和日志。")

    def attach(self, task_id: str) -> None:
        command = self._command(task_id, "attach-session", "-E", "-t", "=work")
        try:
            result = subprocess.run(command, env=self._environment(), check=False)
        except OSError as exc:
            raise WorkspaceError(f"无法进入会话：{exc}") from exc
        if result.returncode != 0:
            raise WorkspaceError("无法进入会话。任务可能已经结束，可以查看任务日志。")

    def is_alive(self, task_id: str) -> bool | None:
        if not self.tmux:
            return None
        result = self._call(self._command(task_id, "has-session", "-t", "=work"))
        if result.returncode == 0:
            return True
        error = result.stderr.lower()
        if any(message in error for message in (
            "no server running", "can't find session", "no such file or directory", "connection refused",
        )):
            return False
        return None

    def refresh(self) -> tuple[dict[str, RunRecord], list[str]]:
        running: dict[str, RunRecord] = {}
        recovered: list[str] = []
        for run in self.service.list_active_workbench_runs():
            alive = self.is_alive(run.task_id)
            if alive is False:
                if self.service.recover_workbench_run(run):
                    recovered.append(run.task_id)
            else:
                running[run.task_id] = run
        return running, recovered
