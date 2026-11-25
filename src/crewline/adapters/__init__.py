"""Agent adapters layer decoupled from task state and CLI commands."""

from crewline.adapters.base import (
    AgentAdapter,
    ExecutionRequest,
    ExecutionResult,
    ProviderSpec,
    build_task_prompt,
)
from crewline.adapters.registry import AdapterRegistry, create_default_registry

__all__ = [
    "AdapterRegistry",
    "AgentAdapter",
    "ExecutionRequest",
    "ExecutionResult",
    "ProviderSpec",
    "build_task_prompt",
    "create_default_registry",
]
