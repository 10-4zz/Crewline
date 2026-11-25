"""Registry mapping provider IDs to their adapter implementations."""

from __future__ import annotations

from crewline.adapters.base import AgentAdapter, ProviderSpec
from crewline.adapters.claude_code import ClaudeCodeAdapter
from crewline.adapters.codex import CodexAdapter
from crewline.adapters.deepseek import DeepSeekAdapter
from crewline.adapters.gemini import GeminiAdapter
from crewline.adapters.grok_build import GrokBuildAdapter
from crewline.adapters.mock import MockAdapter
from crewline.domain.errors import AdapterError
from crewline.domain.models import ProviderId


class AdapterRegistry:
    """Extensible registry of agent adapters."""

    def __init__(self) -> None:
        self._adapters: dict[ProviderId, AgentAdapter] = {}

    def register(self, adapter: AgentAdapter) -> None:
        self._adapters[adapter.spec.provider] = adapter

    def get(self, provider: ProviderId) -> AgentAdapter:
        adapter = self._adapters.get(provider)
        if adapter is None:
            raise AdapterError(f"未注册厂商适配器：{provider.value}")
        return adapter

    def list_specs(self) -> list[ProviderSpec]:
        return [adapter.spec for adapter in self._adapters.values()]


def create_default_registry() -> AdapterRegistry:
    """Create the default v0.1 registry with all built-in adapters."""
    registry = AdapterRegistry()
    registry.register(MockAdapter())
    registry.register(CodexAdapter())
    registry.register(ClaudeCodeAdapter())
    registry.register(GrokBuildAdapter())
    registry.register(GeminiAdapter())
    registry.register(DeepSeekAdapter())
    return registry
