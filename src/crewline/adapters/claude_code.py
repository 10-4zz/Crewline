"""Anthropic Claude Code official CLI adapter (`claude -p`)."""

from __future__ import annotations

from crewline.adapters.base import (
    AgentAdapter,
    ExecutionRequest,
    ExecutionResult,
    ProviderSpec,
)
from crewline.adapters.cli_common import (
    find_cli_binary,
    probe_cli_command,
    run_cli_subprocess,
)
from crewline.domain.capabilities import AgentCapability
from crewline.domain.models import AgentConfig, AuthMethod, DoctorItem, ProviderId


class ClaudeCodeAdapter(AgentAdapter):
    """Executes tasks through the user's installed official `claude` CLI."""

    spec = ProviderSpec(
        provider=ProviderId.CLAUDE_CODE,
        display_name="Anthropic Claude Code CLI",
        auth_method=AuthMethod.OFFICIAL_CLI,
        capabilities=(
            AgentCapability.ANALYSIS,
            AgentCapability.EDIT_FILES,
            AgentCapability.RUN_TESTS,
        ),
        default_model="claude-sonnet-4-5",
        default_cli_bin="claude",
    )

    def _binary(self, agent: AgentConfig) -> str:
        return agent.cli_bin or self.spec.default_cli_bin or "claude"

    def check_health(self, agent: AgentConfig) -> DoctorItem:
        cli_bin = self._binary(agent)
        model_display = agent.model or self.spec.default_model
        resolved = find_cli_binary(cli_bin)
        if not resolved:
            return DoctorItem(
                agent_name=agent.name,
                provider=self.spec.provider.value,
                model=model_display,
                auth_method=agent.auth_method.value,
                capabilities=list(self.spec.capabilities),
                ready=False,
                status_label="MISSING_CLI",
                summary=f"未检测到官方 CLI `{cli_bin}`。",
                missing_items=[f"缺少 CLI: {cli_bin}"],
                setup_steps=[
                    "安装 Anthropic Claude Code 官方 CLI：npm install -g @anthropic-ai/claude-code",
                    "启动官方 CLI 完成账号登录：claude（或 claude auth login）",
                    "验证登录状态：claude auth status",
                ],
                credential_ref=f"official_cli:{cli_bin}",
            )

        code, output = probe_cli_command([resolved, "auth", "status"])
        lowered = output.lower()
        if code == 0 and "not logged in" not in lowered and '"loggedIn": false' not in lowered:
            return DoctorItem(
                agent_name=agent.name,
                provider=self.spec.provider.value,
                model=model_display,
                auth_method=agent.auth_method.value,
                capabilities=list(self.spec.capabilities),
                ready=True,
                status_label="READY",
                summary=f"已检测到 `{cli_bin}` 且官方登录有效。",
                missing_items=[],
                setup_steps=[],
                credential_ref=f"official_cli:{cli_bin}",
            )

        return DoctorItem(
            agent_name=agent.name,
            provider=self.spec.provider.value,
            model=model_display,
            auth_method=agent.auth_method.value,
            capabilities=list(self.spec.capabilities),
            ready=False,
            status_label="NOT_LOGGED_IN",
            summary=f"已安装 `{cli_bin}`，但官方登录未就绪（{output or '无输出'}）。",
            missing_items=["Claude Code 官方 CLI 未登录"],
            setup_steps=[
                f"执行官方登录：{cli_bin} auth login（或交互运行 `{cli_bin}` 后执行 `/login`）",
                f"确认登录状态：{cli_bin} auth status",
            ],
            credential_ref=f"official_cli:{cli_bin}",
        )

    def build_command(self, resolved_bin: str, request: ExecutionRequest) -> list[str]:
        cmd = [
            resolved_bin,
            "-p",
            request.prompt,
            "--output-format",
            "text",
        ]
        if request.agent.model:
            cmd.extend(["--model", request.agent.model])
        if request.agent.extra_args:
            cmd.extend(request.agent.extra_args)
        return cmd

    def execute(self, request: ExecutionRequest) -> ExecutionResult:
        cli_bin = self._binary(request.agent)
        resolved = find_cli_binary(cli_bin)
        if not resolved:
            err = f"未找到 Claude Code 官方 CLI `{cli_bin}`，请先运行 `crewline doctor` 查看安装与登录步骤。"
            request.log_callback(err)
            return ExecutionResult(
                success=False,
                exit_code=127,
                summary="Claude Code CLI 未安装",
                error_message=err,
            )

        cmd = self.build_command(resolved, request)
        request.log_callback(
            f"[claude_code] 执行命令: {resolved} -p <prompt> --output-format text"
            + (f" --model {request.agent.model}" if request.agent.model else "")
        )
        exit_code, lines, timed_out = run_cli_subprocess(
            cmd=cmd,
            cwd=request.cwd,
            timeout_seconds=request.timeout_seconds,
            log_callback=request.log_callback,
            redactor=request.redactor,
        )
        output_text = "\n".join(lines).strip()
        if exit_code == 0 and not timed_out:
            summary = (lines[-1] if lines else "Claude Code 执行完成")[:240]
            return ExecutionResult(
                success=True,
                exit_code=0,
                summary=summary,
                output_text=output_text,
            )

        err_tail = "\n".join(lines[-5:]).strip() if lines else f"claude 退出码 {exit_code}"
        return ExecutionResult(
            success=False,
            exit_code=exit_code,
            summary="Claude Code 执行失败",
            output_text=output_text,
            error_message=err_tail[:500],
        )
