"""Domain layer: models, capabilities, state machine, and typed errors."""

from crewline.domain.capabilities import AgentCapability, capability_labels
from crewline.domain.errors import (
    AdapterError,
    AgentNotFoundError,
    CardAlreadyRunningError,
    ConfigError,
    CrewlineError,
    CredentialSecurityError,
    InvalidStateTransitionError,
    TaskNotFoundError,
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
    RunStatus,
    TaskCard,
    TaskStatus,
    WorkspaceChanges,
)
from crewline.domain.state_machine import (
    VALID_TRANSITIONS,
    can_transition,
    ensure_transition,
)

__all__ = [
    "AdapterError",
    "AgentCapability",
    "AgentConfig",
    "AgentNotFoundError",
    "AuthMethod",
    "CardAlreadyRunningError",
    "ConfigError",
    "CrewlineError",
    "CredentialSecurityError",
    "DoctorItem",
    "InvalidStateTransitionError",
    "ProviderId",
    "ReviewDecision",
    "ReviewRecord",
    "RunRecord",
    "RunStatus",
    "TaskCard",
    "TaskStatus",
    "VALID_TRANSITIONS",
    "WorkspaceChanges",
    "WorkspaceError",
    "can_transition",
    "capability_labels",
    "ensure_transition",
]
