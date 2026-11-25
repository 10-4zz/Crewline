"""Load, validate, and save Crewline TOML configuration safely."""

from __future__ import annotations

import json
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from crewline.domain.errors import ConfigError
from crewline.domain.models import AgentConfig, AuthMethod, ProviderId
from crewline.security.redactor import reject_forbidden_secret_keys, validate_env_var_name

CONFIG_FILENAME = "config.toml"
_AGENT_NAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.\-]{0,63}$")

PROVIDER_ALLOWED_AUTH: dict[ProviderId, frozenset[AuthMethod]] = {
    ProviderId.MOCK: frozenset({AuthMethod.NONE}),
    ProviderId.CODEX: frozenset({AuthMethod.OFFICIAL_CLI}),
    ProviderId.CLAUDE_CODE: frozenset({AuthMethod.OFFICIAL_CLI}),
    ProviderId.GROK_BUILD: frozenset({AuthMethod.OFFICIAL_CLI}),
    ProviderId.GEMINI: frozenset({AuthMethod.API_KEY}),
    ProviderId.DEEPSEEK: frozenset({AuthMethod.API_KEY}),
}

PROVIDER_DEFAULT_ENV: dict[ProviderId, str] = {
    ProviderId.GEMINI: "GEMINI_API_KEY",
    ProviderId.DEEPSEEK: "DEEPSEEK_API_KEY",
}

PROVIDER_DEFAULT_MODEL: dict[ProviderId, str] = {
    ProviderId.MOCK: "mock-v1",
    ProviderId.CODEX: "",
    ProviderId.CLAUDE_CODE: "claude-sonnet-4-5",
    ProviderId.GROK_BUILD: "grok-code-fast-1",
    ProviderId.GEMINI: "gemini-2.5-flash",
    ProviderId.DEEPSEEK: "deepseek-chat",
}


@dataclass(slots=True)
class CrewlineConfig:
    """Top-level Crewline configuration."""

    version: str = "0.1"
    default_timeout_seconds: int = 300
    agents: dict[str, AgentConfig] = field(default_factory=dict)

    def env_var_refs(self) -> list[str]:
        """Return all configured credential environment variable names."""
        refs: list[str] = []
        for agent in self.agents.values():
            if agent.api_key_env:
                refs.append(agent.api_key_env)
        return refs


def validate_agent_config(agent: AgentConfig) -> AgentConfig:
    """Validate an AgentConfig's name, provider, auth method, and credential reference."""
    if not _AGENT_NAME_RE.match(agent.name):
        raise ConfigError(
            f"无效的 Agent 名称 '{agent.name}'。仅允许字母、数字、点、短横线和下划线。"
        )
    allowed_auth = PROVIDER_ALLOWED_AUTH[agent.provider]
    if agent.auth_method not in allowed_auth:
        allowed_str = ", ".join(a.value for a in sorted(allowed_auth, key=lambda x: x.value))
        raise ConfigError(
            f"厂商 '{agent.provider.value}' 不支持认证方式 '{agent.auth_method.value}'，"
            f"官方支持的方式：{allowed_str}。"
        )
    if agent.auth_method == AuthMethod.API_KEY:
        env_ref = agent.api_key_env or PROVIDER_DEFAULT_ENV.get(agent.provider)
        if not env_ref:
            raise ConfigError(f"Agent '{agent.name}' 使用 api_key 认证时必须指定 api_key_env。")
        agent.api_key_env = validate_env_var_name(env_ref)
    else:
        if agent.api_key_env:
            agent.api_key_env = validate_env_var_name(agent.api_key_env)
    return agent


def default_agents() -> dict[str, AgentConfig]:
    """Build default starter agents including zero-account mock agents and official providers."""
    items = [
        AgentConfig(
            name="mock-dev",
            provider=ProviderId.MOCK,
            model="mock-v1",
            auth_method=AuthMethod.NONE,
            description="无需账号的本地模拟开发 Agent（用于演示和测试工作流）",
        ),
        AgentConfig(
            name="mock-qa",
            provider=ProviderId.MOCK,
            model="mock-v1",
            auth_method=AuthMethod.NONE,
            description="无需账号的本地模拟测试 Agent（用于演示多 Agent 并行）",
        ),
        AgentConfig(
            name="codex",
            provider=ProviderId.CODEX,
            model="",
            auth_method=AuthMethod.OFFICIAL_CLI,
            cli_bin="codex",
            description="OpenAI Codex 官方 CLI（由 codex login 管理登录）",
        ),
        AgentConfig(
            name="claude",
            provider=ProviderId.CLAUDE_CODE,
            model="claude-sonnet-4-5",
            auth_method=AuthMethod.OFFICIAL_CLI,
            cli_bin="claude",
            description="Anthropic Claude Code 官方 CLI（由 claude 管理登录）",
        ),
        AgentConfig(
            name="grok",
            provider=ProviderId.GROK_BUILD,
            model="grok-code-fast-1",
            auth_method=AuthMethod.OFFICIAL_CLI,
            cli_bin="grok",
            description="xAI Grok Build 官方 CLI（由 grok 管理登录）",
        ),
        AgentConfig(
            name="gemini",
            provider=ProviderId.GEMINI,
            model="gemini-2.5-flash",
            auth_method=AuthMethod.API_KEY,
            api_key_env="GEMINI_API_KEY",
            description="Google Gemini 官方 API（通过环境变量引用 API Key）",
        ),
        AgentConfig(
            name="deepseek",
            provider=ProviderId.DEEPSEEK,
            model="deepseek-chat",
            auth_method=AuthMethod.API_KEY,
            api_key_env="DEEPSEEK_API_KEY",
            description="DeepSeek 官方 API（通过环境变量引用 API Key）",
        ),
    ]
    return {item.name: validate_agent_config(item) for item in items}


def load_config(config_path: Path) -> CrewlineConfig:
    """Load and validate a Crewline TOML config file."""
    if not config_path.exists():
        raise ConfigError(
            f"未找到配置文件 {config_path}。请先运行 `crewline init` 初始化本地工作区。"
        )
    try:
        raw = tomllib.loads(config_path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"配置文件解析失败 ({config_path}): {exc}") from exc

    reject_forbidden_secret_keys(raw, context=str(config_path))
    crewline_section = raw.get("crewline", {})
    if not isinstance(crewline_section, dict):
        raise ConfigError("配置节 [crewline] 格式不正确。")
    reject_forbidden_secret_keys(crewline_section, context="[crewline]")

    version = str(crewline_section.get("version", "0.1"))
    timeout = int(crewline_section.get("default_timeout_seconds", 300))

    raw_agents = raw.get("agents", {})
    if not isinstance(raw_agents, dict):
        raise ConfigError("配置节 [agents] 格式不正确。")

    agents: dict[str, AgentConfig] = {}
    for name, entry in raw_agents.items():
        if not isinstance(entry, dict):
            raise ConfigError(f"Agent 配置 [agents.{name}] 必须是表结构。")
        reject_forbidden_secret_keys(entry, context=f"[agents.{name}]")
        try:
            provider = ProviderId(str(entry["provider"]))
        except (KeyError, ValueError) as exc:
            valid = ", ".join(p.value for p in ProviderId)
            raise ConfigError(
                f"Agent '{name}' 的 provider 无效。可选值：{valid}"
            ) from exc

        default_auth = next(iter(PROVIDER_ALLOWED_AUTH[provider]))
        auth_raw = entry.get("auth_method", default_auth.value)
        try:
            auth_method = AuthMethod(str(auth_raw))
        except ValueError as exc:
            valid_auth = ", ".join(a.value for a in AuthMethod)
            raise ConfigError(
                f"Agent '{name}' 的 auth_method 无效。可选值：{valid_auth}"
            ) from exc

        extra_args_raw = entry.get("extra_args", [])
        if not isinstance(extra_args_raw, list) or not all(
            isinstance(x, str) for x in extra_args_raw
        ):
            raise ConfigError(f"Agent '{name}' 的 extra_args 必须是字符串列表。")

        agent = AgentConfig(
            name=name,
            provider=provider,
            model=str(entry.get("model", PROVIDER_DEFAULT_MODEL[provider])),
            auth_method=auth_method,
            api_key_env=entry.get("api_key_env"),
            cli_bin=entry.get("cli_bin"),
            base_url=entry.get("base_url"),
            extra_args=list(extra_args_raw),
            description=str(entry.get("description", "")),
        )
        agents[name] = validate_agent_config(agent)

    return CrewlineConfig(
        version=version,
        default_timeout_seconds=timeout,
        agents=agents,
    )


def _toml_str(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def _toml_key(name: str) -> str:
    if re.match(r"^[A-Za-z0-9_\-]+$", name):
        return name
    return _toml_str(name)


def save_config(config_path: Path, config: CrewlineConfig) -> None:
    """Serialize and write Crewline configuration to TOML (credential references only)."""
    config_path.parent.mkdir(parents=True, exist_ok=True)
    lines: list[str] = [
        "# Crewline v0.1 配置文件",
        "# 安全说明：本文件只保存凭证引用（如 api_key_env 环境变量名），绝不保存任何 API Key、密码或登录令牌。",
        "",
        "[crewline]",
        f"version = {_toml_str(config.version)}",
        f"default_timeout_seconds = {int(config.default_timeout_seconds)}",
        "",
    ]
    for name, agent in config.agents.items():
        validate_agent_config(agent)
        lines.append(f"[agents.{_toml_key(name)}]")
        lines.append(f"provider = {_toml_str(agent.provider.value)}")
        lines.append(f"model = {_toml_str(agent.model)}")
        lines.append(f"auth_method = {_toml_str(agent.auth_method.value)}")
        if agent.api_key_env:
            lines.append(f"api_key_env = {_toml_str(agent.api_key_env)}")
        if agent.cli_bin:
            lines.append(f"cli_bin = {_toml_str(agent.cli_bin)}")
        if agent.base_url:
            lines.append(f"base_url = {_toml_str(agent.base_url)}")
        if agent.extra_args:
            args_str = ", ".join(_toml_str(a) for a in agent.extra_args)
            lines.append(f"extra_args = [{args_str}]")
        if agent.description:
            lines.append(f"description = {_toml_str(agent.description)}")
        lines.append("")

    config_path.write_text("\n".join(lines), encoding="utf-8")
