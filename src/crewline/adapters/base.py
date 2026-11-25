"""Base abstractions for Crewline agent adapters."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from crewline.domain.capabilities import AgentCapability
from crewline.domain.models import AgentConfig, AuthMethod, DoctorItem, ProviderId, TaskCard
from crewline.security.redactor import SecretRedactor

LogCallback = Callable[[str], None]


@dataclass(frozen=True, slots=True)
class ProviderSpec:
    """Static metadata describing a supported provider."""

    provider: ProviderId
    display_name: str
    auth_method: AuthMethod
    capabilities: tuple[AgentCapability, ...]
    default_model: str
    default_cli_bin: str | None = None
    default_api_key_env: str | None = None


@dataclass(slots=True)
class ExecutionRequest:
    """Inputs passed from the engine to an agent adapter."""

    task: TaskCard
    run_id: str
    agent: AgentConfig
    cwd: Path
    prompt: str
    timeout_seconds: int
    log_callback: LogCallback
    redactor: SecretRedactor
    is_git_worktree: bool = False
    worktree_branch: str | None = None
    prior_runs_count: int = 0


@dataclass(slots=True)
class ExecutionResult:
    """Normalized execution result returned by an adapter."""

    success: bool
    exit_code: int
    summary: str
    output_text: str = ""
    error_message: str | None = None
    metadata: dict[str, str] = field(default_factory=dict)


def build_task_prompt(
    task: TaskCard,
    is_git_worktree: bool,
    capabilities: tuple[AgentCapability, ...],
) -> str:
    """Build a structured task prompt including prior review feedback if retrying."""
    lines: list[str] = [
        f"任务卡 ID: {task.id}",
        f"任务标题: {task.title}",
        "",
        "任务描述:",
        task.description.strip() or "(无补充描述)",
    ]

    if task.last_review_comment:
        lines.extend(
            [
                "",
                "人工审核退回意见（请重点处理并改进）:",
                task.last_review_comment.strip(),
            ]
        )

    if task.last_error:
        lines.extend(
            [
                "",
                "上一次执行失败原因（请避免重复出错）:",
                task.last_error.strip(),
            ]
        )

    lines.append("")
    if AgentCapability.EDIT_FILES in capabilities:
        if is_git_worktree:
            lines.append(
                "执行要求：你正在该任务卡专属的独立 Git worktree 中工作。请直接完成任务要求的修改和验证，并输出简明中文结果摘要。"
            )
        else:
            lines.append(
                "执行要求：请在当前工作区完成任务要求的修改和验证，并输出简明中文结果摘要。"
            )
    else:
        lines.append(
            "执行要求：请基于任务描述输出清晰、可执行的中文分析结果与实现建议（当前 Agent 仅输出分析结果，不直接修改本地文件）。"
        )

    return "\n".join(lines)


class AgentAdapter(ABC):
    """Abstract interface implemented by each provider adapter."""

    spec: ProviderSpec

    @abstractmethod
    def check_health(self, agent: AgentConfig) -> DoctorItem:
        """Inspect CLI / login / credential readiness for the given named agent."""

    @abstractmethod
    def execute(self, request: ExecutionRequest) -> ExecutionResult:
        """Execute a task card run and stream redacted progress via log_callback."""
