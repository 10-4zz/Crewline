"""Google Gemini official API Key adapter (v0.1 analysis mode)."""

from __future__ import annotations

from crewline.adapters.api_common import post_json, resolve_api_key_from_env
from crewline.adapters.base import (
    AgentAdapter,
    ExecutionRequest,
    ExecutionResult,
    ProviderSpec,
)
from crewline.domain.capabilities import AgentCapability
from crewline.domain.errors import AdapterError
from crewline.domain.models import AgentConfig, AuthMethod, DoctorItem, ProviderId

DEFAULT_GEMINI_BASE_URL = "https://generativelanguage.googleapis.com/v1beta"


class GeminiAdapter(AgentAdapter):
    """Calls the official Google Gemini REST API using a user-provided API Key env var."""

    spec = ProviderSpec(
        provider=ProviderId.GEMINI,
        display_name="Google Gemini API",
        auth_method=AuthMethod.API_KEY,
        capabilities=(AgentCapability.ANALYSIS,),
        default_model="gemini-2.5-flash",
        default_api_key_env="GEMINI_API_KEY",
    )

    def check_health(self, agent: AgentConfig) -> DoctorItem:
        env_name, api_key = resolve_api_key_from_env(agent, "GEMINI_API_KEY")
        model = agent.model or self.spec.default_model
        if api_key:
            return DoctorItem(
                agent_name=agent.name,
                provider=self.spec.provider.value,
                model=model,
                auth_method=agent.auth_method.value,
                capabilities=list(self.spec.capabilities),
                ready=True,
                status_label="READY",
                summary=f"已检测到环境变量 `{env_name}`（不会读取或记录密钥内容）。",
                missing_items=[],
                setup_steps=[],
                credential_ref=f"env:{env_name}",
            )

        return DoctorItem(
            agent_name=agent.name,
            provider=self.spec.provider.value,
            model=model,
            auth_method=agent.auth_method.value,
            capabilities=list(self.spec.capabilities),
            ready=False,
            status_label="MISSING_ENV",
            summary=f"缺少环境变量 `{env_name}`。",
            missing_items=[f"缺少凭证环境变量: {env_name}"],
            setup_steps=[
                "在 Google AI Studio 创建 Gemini API Key",
                f"在当前终端导出环境变量：export {env_name}='你的APIKey'",
                "重新运行 `crewline doctor` 验证就绪状态（切勿将 Key 写入 config.toml）",
            ],
            credential_ref=f"env:{env_name}",
        )

    def execute(self, request: ExecutionRequest) -> ExecutionResult:
        env_name, api_key = resolve_api_key_from_env(request.agent, "GEMINI_API_KEY")
        if not api_key:
            err = f"未设置环境变量 `{env_name}`，无法调用 Gemini API。请先运行 `crewline doctor` 查看配置步骤。"
            request.log_callback(err)
            return ExecutionResult(
                success=False,
                exit_code=2,
                summary=f"缺少环境变量 {env_name}",
                error_message=err,
            )

        model = request.agent.model or self.spec.default_model
        base_url = (request.agent.base_url or DEFAULT_GEMINI_BASE_URL).rstrip("/")
        url = f"{base_url}/models/{model}:generateContent"

        request.log_callback(
            f"[gemini] 调用模型 `{model}`（endpoint={url}，凭证引用=env:{env_name}）"
        )
        payload = {
            "contents": [
                {
                    "role": "user",
                    "parts": [{"text": request.prompt}],
                }
            ]
        }

        try:
            _, data = post_json(
                url=url,
                payload=payload,
                headers={"x-goog-api-key": api_key},
                timeout_seconds=request.timeout_seconds,
                redactor=request.redactor,
            )
        except AdapterError as exc:
            err = request.redactor.redact_str(str(exc))
            request.log_callback(f"[gemini] ERROR: {err}")
            return ExecutionResult(
                success=False,
                exit_code=1,
                summary="Gemini API 调用失败",
                error_message=err,
            )

        text = self._extract_text(data)
        text = request.redactor.redact_str(text).strip()
        if not text:
            err = "Gemini API 返回了空内容。"
            request.log_callback(f"[gemini] ERROR: {err}")
            return ExecutionResult(
                success=False,
                exit_code=1,
                summary="Gemini 返回空内容",
                error_message=err,
            )

        request.log_callback("[gemini] === 模型分析输出 ===")
        for line in text.splitlines():
            request.log_callback(line)

        summary = text.splitlines()[0][:240]
        return ExecutionResult(
            success=True,
            exit_code=0,
            summary=summary,
            output_text=text,
        )

    @staticmethod
    def _extract_text(data: dict[str, object]) -> str:
        candidates = data.get("candidates")
        if not isinstance(candidates, list) or not candidates:
            return ""
        first = candidates[0]
        if not isinstance(first, dict):
            return ""
        content = first.get("content")
        if not isinstance(content, dict):
            return ""
        parts = content.get("parts")
        if not isinstance(parts, list):
            return ""
        chunks: list[str] = []
        for part in parts:
            if isinstance(part, dict) and isinstance(part.get("text"), str):
                chunks.append(part["text"])
        return "\n".join(chunks)
