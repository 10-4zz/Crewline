"""DeepSeek official API Key adapter (v0.1 analysis mode)."""

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

DEFAULT_DEEPSEEK_BASE_URL = "https://api.deepseek.com"


class DeepSeekAdapter(AgentAdapter):
    """Calls the official DeepSeek Chat Completions API using a user-provided API Key env var."""

    spec = ProviderSpec(
        provider=ProviderId.DEEPSEEK,
        display_name="DeepSeek API",
        auth_method=AuthMethod.API_KEY,
        capabilities=(AgentCapability.ANALYSIS,),
        default_model="deepseek-chat",
        default_api_key_env="DEEPSEEK_API_KEY",
    )

    def check_health(self, agent: AgentConfig) -> DoctorItem:
        env_name, api_key = resolve_api_key_from_env(agent, "DEEPSEEK_API_KEY")
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
                "在 DeepSeek 开放平台创建 API Key",
                f"在当前终端导出环境变量：export {env_name}='你的APIKey'",
                "重新运行 `crewline doctor` 验证就绪状态（切勿将 Key 写入 config.toml）",
            ],
            credential_ref=f"env:{env_name}",
        )

    def execute(self, request: ExecutionRequest) -> ExecutionResult:
        env_name, api_key = resolve_api_key_from_env(request.agent, "DEEPSEEK_API_KEY")
        if not api_key:
            err = f"未设置环境变量 `{env_name}`，无法调用 DeepSeek API。请先运行 `crewline doctor` 查看配置步骤。"
            request.log_callback(err)
            return ExecutionResult(
                success=False,
                exit_code=2,
                summary=f"缺少环境变量 {env_name}",
                error_message=err,
            )

        model = request.agent.model or self.spec.default_model
        base_url = (request.agent.base_url or DEFAULT_DEEPSEEK_BASE_URL).rstrip("/")
        url = f"{base_url}/chat/completions"

        request.log_callback(
            f"[deepseek] 调用模型 `{model}`（endpoint={url}，凭证引用=env:{env_name}）"
        )
        payload = {
            "model": model,
            "messages": [
                {
                    "role": "system",
                    "content": "你是 Crewline 调度的工程分析 Agent，请输出清晰、准确、可执行的中文分析与方案。",
                },
                {
                    "role": "user",
                    "content": request.prompt,
                },
            ],
            "stream": False,
        }

        try:
            _, data = post_json(
                url=url,
                payload=payload,
                headers={"Authorization": f"Bearer {api_key}"},
                timeout_seconds=request.timeout_seconds,
                redactor=request.redactor,
            )
        except AdapterError as exc:
            err = request.redactor.redact_str(str(exc))
            request.log_callback(f"[deepseek] ERROR: {err}")
            return ExecutionResult(
                success=False,
                exit_code=1,
                summary="DeepSeek API 调用失败",
                error_message=err,
            )

        text = self._extract_text(data)
        text = request.redactor.redact_str(text).strip()
        if not text:
            err = "DeepSeek API 返回了空内容。"
            request.log_callback(f"[deepseek] ERROR: {err}")
            return ExecutionResult(
                success=False,
                exit_code=1,
                summary="DeepSeek 返回空内容",
                error_message=err,
            )

        request.log_callback("[deepseek] === 模型分析输出 ===")
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
        choices = data.get("choices")
        if not isinstance(choices, list) or not choices:
            return ""
        first = choices[0]
        if not isinstance(first, dict):
            return ""
        message = first.get("message")
        if not isinstance(message, dict):
            return ""
        content = message.get("content")
        if isinstance(content, str) and content.strip():
            return content
        reasoning = message.get("reasoning_content")
        if isinstance(reasoning, str) and reasoning.strip():
            return reasoning
        return ""
