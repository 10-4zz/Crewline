"""Credential reference validation and secret redaction for logs and DB."""

from __future__ import annotations

import os
import re
from typing import Iterable, Mapping

from crewline.domain.errors import CredentialSecurityError

_ENV_VAR_NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]{1,127}$")

_FORBIDDEN_CONFIG_KEYS = frozenset(
    {
        "api_key",
        "apikey",
        "token",
        "access_token",
        "refresh_token",
        "password",
        "passwd",
        "secret",
        "secret_key",
        "cookie",
        "auth_token",
        "authorization",
    }
)

_RAW_SECRET_PREFIXES = (
    "sk-",
    "AIza",
    "ghp_",
    "gho_",
    "xai-",
    "Bearer ",
    "bearer ",
)

_SECRET_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (
        re.compile(r"(?i)(authorization\s*[:=]\s*bearer\s+)([^\s'\"`]+)"),
        r"\1[REDACTED]",
    ),
    (
        re.compile(r"(?i)(bearer\s+)([A-Za-z0-9._\-]{8,})"),
        r"\1[REDACTED]",
    ),
    (
        re.compile(r"(?i)(x-goog-api-key\s*[:=]\s*)([^\s'\"`]+)"),
        r"\1[REDACTED]",
    ),
    (
        re.compile(r"(?i)((?:api[_-]?key|access[_-]?token|refresh[_-]?token|password|secret)\s*[:=]\s*['\"]?)([^\s'\"`&,;]+)"),
        r"\1[REDACTED]",
    ),
    (
        re.compile(r"\bsk-[A-Za-z0-9_\-]{10,}\b"),
        "[REDACTED]",
    ),
    (
        re.compile(r"\bAIza[0-9A-Za-z_\-]{20,}\b"),
        "[REDACTED]",
    ),
    (
        re.compile(r"\bxai-[A-Za-z0-9_\-]{10,}\b"),
        "[REDACTED]",
    ),
)


def validate_env_var_name(value: str) -> str:
    """Validate that a credential reference is an environment variable name, not a secret."""
    candidate = value.strip()
    if any(candidate.startswith(prefix) for prefix in _RAW_SECRET_PREFIXES):
        raise CredentialSecurityError(
            "配置中只能保存环境变量名（例如 GEMINI_API_KEY），禁止直接填写 API Key 或令牌。"
        )
    if not _ENV_VAR_NAME_RE.match(candidate):
        raise CredentialSecurityError(
            f"无效的环境变量引用名称 '{value}'。请使用大写字母、数字和下划线组成的环境变量名"
            "（例如 GEMINI_API_KEY、DEEPSEEK_API_KEY），不要直接填写密钥值。"
        )
    return candidate


def reject_forbidden_secret_keys(data: Mapping[str, object], context: str = "config") -> None:
    """Reject configuration mappings that contain raw secret fields."""
    for key in data:
        normalized = str(key).strip().lower()
        if normalized in _FORBIDDEN_CONFIG_KEYS:
            raise CredentialSecurityError(
                f"{context} 包含禁止保存的敏感字段 '{key}'。"
                "Crewline 只允许保存凭证引用（如 api_key_env），禁止将密钥、密码或令牌写入配置。"
            )


class SecretRedactor:
    """Redacts known runtime secret values and secret-like patterns from text."""

    def __init__(
        self,
        env_var_names: Iterable[str] = (),
        extra_secrets: Iterable[str] = (),
        environ: Mapping[str, str] | None = None,
    ) -> None:
        env = environ if environ is not None else os.environ
        values: set[str] = set()
        for name in env_var_names:
            if not name:
                continue
            val = env.get(name)
            if val and len(val) >= 6:
                values.add(val)
        for secret in extra_secrets:
            if secret and len(secret) >= 6:
                values.add(secret)
        # Replace longer secrets first to avoid partial overlap leftovers.
        self._literal_secrets = sorted(values, key=len, reverse=True)

    def redact(self, text: str | None) -> str | None:
        """Scrub all literal secrets and secret patterns from text."""
        if text is None:
            return None
        result = text
        for secret in self._literal_secrets:
            if secret in result:
                result = result.replace(secret, "[REDACTED]")
        for pattern, replacement in _SECRET_PATTERNS:
            result = pattern.sub(replacement, result)
        return result

    def redact_str(self, text: str) -> str:
        """Scrub secrets from a non-null string."""
        cleaned = self.redact(text)
        return cleaned if cleaned is not None else ""
