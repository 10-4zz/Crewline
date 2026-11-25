"""Zero-account local mock adapter for demos, offline workflow validation, and tests."""

from __future__ import annotations

import re
import time
from pathlib import Path

from crewline.adapters.base import (
    AgentAdapter,
    ExecutionRequest,
    ExecutionResult,
    ProviderSpec,
)
from crewline.domain.capabilities import AgentCapability
from crewline.domain.models import AgentConfig, AuthMethod, DoctorItem, ProviderId

_DELAY_RE = re.compile(r"\[mock:delay=([0-9]+(?:\.[0-9]+)?)\]")
_FILE_RE = re.compile(r"\[mock:file=([A-Za-z0-9_./\-]+)\]")


class MockAdapter(AgentAdapter):
    """Deterministic zero-account agent supporting file edits, retries, and simulated failures."""

    spec = ProviderSpec(
        provider=ProviderId.MOCK,
        display_name="Mock Agent (本地模拟)",
        auth_method=AuthMethod.NONE,
        capabilities=(
            AgentCapability.ANALYSIS,
            AgentCapability.EDIT_FILES,
            AgentCapability.RUN_TESTS,
        ),
        default_model="mock-v1",
    )

    def check_health(self, agent: AgentConfig) -> DoctorItem:
        return DoctorItem(
            agent_name=agent.name,
            provider=self.spec.provider.value,
            model=agent.model or self.spec.default_model,
            auth_method=agent.auth_method.value,
            capabilities=list(self.spec.capabilities),
            ready=True,
            status_label="READY",
            summary="本地模拟 Agent，无需账号或 API Key，随时可用。",
            missing_items=[],
            setup_steps=[],
            credential_ref=None,
        )

    def execute(self, request: ExecutionRequest) -> ExecutionResult:
        desc = request.task.description
        request.log_callback(
            f"[mock:{request.agent.name}] 开始执行任务卡 {request.task.id}（run={request.run_id}）"
        )
        request.log_callback(
            f"[mock:{request.agent.name}] 工作目录: {request.cwd}"
            + (
                f" (Git worktree 分支: {request.worktree_branch})"
                if request.is_git_worktree
                else " (非 Git 目录)"
            )
        )

        delay_match = _DELAY_RE.search(desc)
        if delay_match:
            delay_seconds = min(float(delay_match.group(1)), 10.0)
            request.log_callback(
                f"[mock:{request.agent.name}] 模拟耗时处理 {delay_seconds:.2f}s..."
            )
            time.sleep(delay_seconds)

        if "[mock:fail]" in desc:
            err = "模拟执行失败：任务描述包含 [mock:fail] 触发了错误。"
            request.log_callback(f"[mock:{request.agent.name}] ERROR: {err}")
            return ExecutionResult(
                success=False,
                exit_code=1,
                summary="模拟执行失败",
                error_message=err,
            )

        if "[mock:fail-once]" in desc and request.prior_runs_count == 0:
            err = "模拟首次执行失败（[mock:fail-once]），重试后将成功。"
            request.log_callback(f"[mock:{request.agent.name}] ERROR: {err}")
            return ExecutionResult(
                success=False,
                exit_code=1,
                summary="首次模拟执行失败，等待重试",
                error_message=err,
            )

        file_match = _FILE_RE.search(desc)
        rel_path = file_match.group(1) if file_match else f"crewline_outputs/{request.task.id}.md"
        target_path = (request.cwd / rel_path).resolve()
        # Prevent path traversal outside the task workspace.
        cwd_resolved = request.cwd.resolve()
        if cwd_resolved not in target_path.parents and target_path != cwd_resolved:
            err = f"非法目标文件路径：{rel_path} 超出工作区目录。"
            request.log_callback(f"[mock:{request.agent.name}] ERROR: {err}")
            return ExecutionResult(
                success=False,
                exit_code=1,
                summary="目标路径校验失败",
                error_message=err,
            )

        target_path.parent.mkdir(parents=True, exist_ok=True)
        review_section = ""
        if request.task.last_review_comment:
            request.log_callback(
                f"[mock:{request.agent.name}] 检测到人工审核退回意见：{request.task.last_review_comment}"
            )
            review_section = (
                "\n## 审核意见响应\n\n"
                f"- 收到退回意见：{request.task.last_review_comment}\n"
                f"- 改进说明：已在 {request.run_id} 中根据审核意见补充完善内容并复测通过。\n"
            )

        content = (
            f"# {request.task.title}\n\n"
            f"- 任务卡：`{request.task.id}`\n"
            f"- 执行 Agent：`{request.agent.name}` (`{request.agent.model}`)\n"
            f"- 运行记录：`{request.run_id}`\n"
            f"- 分支：`{request.worktree_branch or 'local'}`\n\n"
            "## 任务说明\n\n"
            f"{request.task.description.strip() or '(无补充说明)'}\n\n"
            "## 模拟交付结果\n\n"
            f"- 已在独立工作区生成交付文件 `{rel_path}`。\n"
            "- 模拟单元测试检查：PASSED (1/1)\n"
            f"{review_section}"
        )
        target_path.write_text(content, encoding="utf-8")

        request.log_callback(f"[mock:{request.agent.name}] 已写入工作区文件: {rel_path}")
        request.log_callback(f"[mock:{request.agent.name}] 模拟验证检查: PASSED (1/1)")

        summary = f"已完成模拟交付并更新 `{rel_path}`"
        if request.task.last_review_comment:
            summary += "（已响应人工审核退回意见）"

        request.log_callback(f"[mock:{request.agent.name}] 执行成功：{summary}")
        return ExecutionResult(
            success=True,
            exit_code=0,
            summary=summary,
            output_text=content,
        )
