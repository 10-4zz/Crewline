"""Run a vendor's interactive CLI in the task's terminal and worktree."""

from __future__ import annotations

import subprocess
import sys

from crewline.adapters.base import ExecutionRequest, ExecutionResult
from crewline.adapters.cli_common import find_cli_binary
from crewline.domain.errors import AdapterError
from crewline.domain.models import ProviderId
from crewline.runtime import clean_subprocess_env


def execute_interactive(
    request: ExecutionRequest, default_cli_bin: str | None
) -> ExecutionResult:
    binary_name = request.agent.cli_bin or default_cli_bin
    binary = find_cli_binary(binary_name) if binary_name else None
    if not binary:
        raise AdapterError(f"找不到官方 CLI {binary_name}，请先安装并登录。")
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        raise AdapterError("官方 CLI 会话需要交互终端。")

    command = [binary]
    if request.agent.model:
        command.extend(["--model", request.agent.model])
    command.extend(request.agent.extra_args)
    if request.agent.provider in (ProviderId.CODEX, ProviderId.CLAUDE_CODE):
        command.append(request.prompt)
    else:
        # Grok Build documents -p as headless. Place the prompt in its worktree
        # session's scrollback for the operator to submit to the interactive UI.
        print("把下面的任务说明交给 Agent（可从 tmux 回滚记录复制）：")
        print(request.redactor.redact_str(request.prompt), flush=True)

    request.log_callback("已打开官方 CLI 会话。退出 Agent 后可选择提交审核。")
    result = subprocess.run(command, cwd=request.cwd, env=clean_subprocess_env(), check=False)
    if result.returncode:
        msg = f"官方 CLI 已退出（退出码 {result.returncode}）。"
        return ExecutionResult(False, result.returncode, msg, error_message=msg)

    try:
        confirmed = input("\n提交当前产出以供审核？[y/N] ").strip().lower() == "y"
    except (EOFError, KeyboardInterrupt):
        confirmed = False
    if confirmed:
        return ExecutionResult(True, 0, "操作者已将交互会话产出提交审核。")
    msg = "会话已退出，任务退回待办。工作区改动保留。"
    return ExecutionResult(False, 1, msg, error_message=msg)
