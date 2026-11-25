"""Core domain models decoupled from CLI and persistence details."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any

from crewline.domain.capabilities import AgentCapability


def utc_now_iso() -> str:
    """Return current UTC timestamp in ISO-8601 format."""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


class TaskStatus(StrEnum):
    """Four lifecycle states of a Crewline task card."""

    TODO = "todo"
    IN_PROGRESS = "in_progress"
    IN_REVIEW = "in_review"
    DONE = "done"

    @property
    def label_zh(self) -> str:
        return _TASK_STATUS_LABELS_ZH[self]


_TASK_STATUS_LABELS_ZH: dict[TaskStatus, str] = {
    TaskStatus.TODO: "待办",
    TaskStatus.IN_PROGRESS: "处理中",
    TaskStatus.IN_REVIEW: "待审核",
    TaskStatus.DONE: "审核完成",
}


class RunStatus(StrEnum):
    """Execution states of a single agent run attempt."""

    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class ReviewDecision(StrEnum):
    """Human review outcomes."""

    APPROVED = "approved"
    REJECTED = "rejected"


class ProviderId(StrEnum):
    """Supported agent providers in Crewline v0.1."""

    MOCK = "mock"
    CODEX = "codex"
    CLAUDE_CODE = "claude_code"
    GROK_BUILD = "grok_build"
    GEMINI = "gemini"
    DEEPSEEK = "deepseek"


class AuthMethod(StrEnum):
    """Official authentication methods supported by Crewline."""

    NONE = "none"
    OFFICIAL_CLI = "official_cli"
    API_KEY = "api_key"


@dataclass(slots=True)
class AgentConfig:
    """Named agent configuration storing ONLY credential references, never secrets."""

    name: str
    provider: ProviderId
    model: str
    auth_method: AuthMethod
    api_key_env: str | None = None
    cli_bin: str | None = None
    base_url: str | None = None
    extra_args: list[str] = field(default_factory=list)
    description: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "provider": self.provider.value,
            "model": self.model,
            "auth_method": self.auth_method.value,
            "api_key_env": self.api_key_env,
            "cli_bin": self.cli_bin,
            "base_url": self.base_url,
            "extra_args": list(self.extra_args),
            "description": self.description,
        }


@dataclass(slots=True)
class TaskCard:
    """A task card managed by Crewline."""

    id: str
    title: str
    description: str
    status: TaskStatus
    assigned_agent: str | None
    repo_path: str
    worktree_path: str | None = None
    worktree_branch: str | None = None
    worktree_base_commit: str | None = None
    last_run_id: str | None = None
    last_error: str | None = None
    last_review_comment: str | None = None
    deleted_at: str | None = None
    created_at: str = field(default_factory=utc_now_iso)
    updated_at: str = field(default_factory=utc_now_iso)

    @property
    def is_deleted(self) -> bool:
        return self.deleted_at is not None

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["status"] = self.status.value
        data["status_zh"] = self.status.label_zh
        data["deleted"] = self.is_deleted
        return data


@dataclass(slots=True)
class RunRecord:
    """A single execution attempt of a task card."""

    id: str
    task_id: str
    agent_name: str
    provider: str
    model: str
    status: RunStatus
    started_at: str
    finished_at: str | None = None
    exit_code: int | None = None
    worktree_path: str | None = None
    worktree_branch: str | None = None
    log_path: str = ""
    summary: str | None = None
    error_message: str | None = None
    changed_files: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["status"] = self.status.value
        return data


@dataclass(slots=True)
class ReviewRecord:
    """A human review decision recorded on a task card."""

    id: str
    task_id: str
    run_id: str | None
    decision: ReviewDecision
    comment: str
    created_at: str = field(default_factory=utc_now_iso)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["decision"] = self.decision.value
        return data


@dataclass(slots=True)
class WorkspaceChanges:
    """Summary of workspace modifications detected after an agent run."""

    is_git_worktree: bool
    changed_files: list[str] = field(default_factory=list)
    diff_stat: str = ""


@dataclass(slots=True)
class DoctorItem:
    """Health and capability diagnostics for a configured agent."""

    agent_name: str
    provider: str
    model: str
    auth_method: str
    capabilities: list[AgentCapability]
    ready: bool
    status_label: str
    summary: str
    missing_items: list[str] = field(default_factory=list)
    setup_steps: list[str] = field(default_factory=list)
    credential_ref: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "agent_name": self.agent_name,
            "provider": self.provider,
            "model": self.model,
            "auth_method": self.auth_method,
            "capabilities": [cap.value for cap in self.capabilities],
            "capabilities_zh": [cap.label_zh for cap in self.capabilities],
            "ready": self.ready,
            "status_label": self.status_label,
            "summary": self.summary,
            "missing_items": list(self.missing_items),
            "setup_steps": list(self.setup_steps),
            "credential_ref": self.credential_ref,
        }
