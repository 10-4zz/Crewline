"""Typed domain errors for Crewline."""


class CrewlineError(Exception):
    """Base error for all Crewline domain and runtime failures."""


class ConfigError(CrewlineError):
    """Raised when Crewline configuration is missing or invalid."""


class CredentialSecurityError(ConfigError):
    """Raised when a raw secret is provided where only a credential reference is allowed."""


class AgentNotFoundError(CrewlineError):
    """Raised when referencing an unconfigured agent name."""


class TaskNotFoundError(CrewlineError):
    """Raised when referencing a non-existent task card."""


class InvalidStateTransitionError(CrewlineError):
    """Raised when a task card state transition is not permitted."""


class CardAlreadyRunningError(InvalidStateTransitionError):
    """Raised when attempting to start a card that is already running."""


class WorkspaceError(CrewlineError):
    """Raised when preparing or inspecting a task execution workspace fails."""


class AdapterError(CrewlineError):
    """Raised when an agent adapter cannot execute or fails configuration checks."""
