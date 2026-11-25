"""Shared HTTP JSON client and env-var credential resolution for API Key adapters."""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any

from crewline.domain.errors import AdapterError
from crewline.domain.models import AgentConfig
from crewline.runtime import build_ssl_context
from crewline.security.redactor import SecretRedactor


def resolve_api_key_from_env(agent: AgentConfig, default_env_name: str) -> tuple[str, str | None]:
    """Resolve an API key strictly from the configured environment variable reference."""
    env_name = agent.api_key_env or default_env_name
    value = os.environ.get(env_name)
    if value is not None:
        value = value.strip() or None
    return env_name, value


def post_json(
    url: str,
    payload: dict[str, Any],
    headers: dict[str, str],
    timeout_seconds: int,
    redactor: SecretRedactor,
) -> tuple[int, dict[str, Any]]:
    """Send a JSON HTTP POST request using stdlib urllib with redacted error messages."""
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req_headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        **headers,
    }
    req = urllib.request.Request(url=url, data=body, headers=req_headers, method="POST")

    try:
        with urllib.request.urlopen(
            req, timeout=timeout_seconds, context=build_ssl_context()
        ) as resp:
            status_code = int(getattr(resp, "status", 200))
            raw = resp.read().decode("utf-8", errors="replace")
            try:
                parsed = json.loads(raw) if raw.strip() else {}
            except json.JSONDecodeError as exc:
                raise AdapterError(
                    redactor.redact_str(f"API 返回了非 JSON 响应 (HTTP {status_code}): {raw[:300]}")
                ) from exc
            if not isinstance(parsed, dict):
                raise AdapterError(f"API 返回了非对象 JSON 结构 (HTTP {status_code})。")
            return status_code, parsed
    except urllib.error.HTTPError as exc:
        err_body = ""
        try:
            err_body = exc.read().decode("utf-8", errors="replace")
        except Exception:
            err_body = str(exc)
        cleaned = redactor.redact_str(err_body.strip()[:400])
        raise AdapterError(f"HTTP {exc.code} 请求失败：{cleaned}") from exc
    except urllib.error.URLError as exc:
        cleaned = redactor.redact_str(str(exc.reason))
        raise AdapterError(f"网络连接失败：{cleaned}") from exc
    except TimeoutError as exc:
        raise AdapterError(f"API 请求超时（>{timeout_seconds}s）。") from exc
