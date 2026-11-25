"""OpenAI Codex official CLI adapter (`codex exec`)."""

from __future__ import annotations

import tempfile
from pathlib import Path

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


class CodexAdapter(AgentAdapter):
    """Executes tasks through the user's installed official `codex` CLI."""

    spec = ProviderSpec(
        provider=ProviderId.CODEX,
        display_name="OpenAI Codex CLI",
        auth_method=AuthMethod.OFFICIAL_CLI,
        capabilities=(
            AgentCapability.ANALYSIS,
            AgentCapability.EDIT_FILES,
            AgentCapability.RUN_TESTS,
        ),
        default_model="",
        default_cli_bin="codex",
    )

    def _binary(self, agent: AgentConfig) -> str:
        return agent.cli_bin or self.spec.default_cli_bin or "codex"

    def check_health(self, agent: AgentConfig) -> DoctorItem:
        cli_bin = self._binary(agent)
        model_display = agent.model or "(CLI 默认)"
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
                    "安装 OpenAI Codex 官方 CLI：npm install -g @openai/codex",
                    "使用官方 CLI 登录：codex login",
                    "验证登录状态：codex login status",
                ],
                credential_ref=f"official_cli:{cli_bin}",
            )

        code, output = probe_cli_command([resolved, "login", "status"])
        lowered = output.lower()
        if code == 0 and "not logged in" not in lowered and "logged in" in lowered:
            return DoctorItem(
                agent_name=agent.name,
                provider=self.spec.provider.value,
                model=model_display,
                auth_method=agent.auth_method.value,
                capabilities=list(self.spec.capabilities),
                ready=True,
                status_label="READY",
                summary=f"已检测到 `{cli_bin}` 且官方登录有效（{output.splitlines()[0]}）。",
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
            summary=f"已安装 `{cli_bin}`，但尚未完成官方登录或无法确认登录状态（{output or '无输出'}）。",
            missing_items=["Codex 官方 CLI 未登录"],
            setup_steps=[
                f"执行官方登录：{cli_bin} login（无浏览器环境可用 `{cli_bin} login --device-auth`）",
                f"确认登录状态：{cli_bin} login status",
            ],
            credential_ref=f"official_cli:{cli_bin}",
        )

    def build_command(
        self,
        resolved_bin: str,
        request: ExecutionRequest,
        output_last_message_file: Path,
    ) -> list[str]:
        has_sandbox_override = any(
            arg in ("-s", "--sandbox", "--dangerously-bypass-approvals-and-sandbox")
            or arg.startswith("--sandbox=")
            for arg in request.agent.extra_args
        )
        cmd = [
            resolved_bin,
            "exec",
            "-C",
            str(request.cwd),
            "--color",
            "never",
            "--ephemeral",
            "-o",
            str(output_last_message_file),
        ]
        if not has_sandbox_override:
            cmd.extend(["-s", "workspace-write"])
        if not (request.cwd / ".git").exists():
            cmd.append("--skip-git-repo-check")
        if request.agent.model:
            cmd.extend(["-m", request.agent.model])
        if request.agent.extra_args:
            cmd.extend(request.agent.extra_args)
        cmd.append("-")
        return cmd

    def execute(self, request: ExecutionRequest) -> ExecutionResult:
        cli_bin = self._binary(request.agent)
        resolved = find_cli_binary(cli_bin)
        if not resolved:
            err = f"未找到 Codex 官方 CLI `{cli_bin}`，请先运行 `crewline doctor` 查看安装与登录步骤。"
            request.log_callback(err)
            return ExecutionResult(
                success=False,
                exit_code=127,
                summary="Codex CLI 未安装",
                error_message=err,
            )

        with tempfile.NamedTemporaryFile(
            prefix="crewline-codex-", suffix=".txt", delete=False
        ) as tmp:
            last_msg_path = Path(tmp.name)

        try:
            cmd = self.build_command(resolved, request, last_msg_path)
            request.log_callback(f"[codex] 执行命令: {' '.join(cmd)}")
            exit_code, lines, timed_out = run_cli_subprocess(
                cmd=cmd,
                cwd=request.cwd,
                timeout_seconds=request.timeout_seconds,
                log_callback=request.log_callback,
                redactor=request.redactor,
                stdin_text=request.prompt,
            )
            last_msg = ""
            if last_msg_path.exists():
                last_msg = request.redactor.redact_str(
                    last_msg_path.read_text(encoding="utf-8", errors="replace").strip()
                )
        finally:
            try:
                last_msg_path.unlink(missing_ok=True)
            except OSError:
                pass

        output_text = last_msg or "\n".join(lines).strip()
        if exit_code == 0 and not timed_out:
            summary_source = last_msg or (lines[-1] if lines else "Codex 执行完成")
            summary = summary_source.splitlines()[0][:240]
            return ExecutionResult(
                success=True,
                exit_code=0,
                summary=summary,
                output_text=output_text,
            )

        err_tail = "\n".join(lines[-5:]).strip() if lines else f"codex 退出码 {exit_code}"
        return ExecutionResult(
            success=False,
            exit_code=exit_code,
            summary="Codex 执行失败",
            output_text=output_text,
            error_message=err_tail[:500],
        )
