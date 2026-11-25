"""Human-readable and JSON formatters for Crewline CLI output."""

from __future__ import annotations

import json
import os
import shutil
import sys
from typing import Any, Mapping

import click

from crewline.domain.capabilities import capability_labels
from crewline.domain.models import AgentConfig, DoctorItem, TaskCard
from crewline.engine.service import RunOutcome, TaskDetail

COMPACT_BANNER_WIDTH = 48
TAGLINE_ZH = "多 Agent 任务管理"


def to_json(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2)


def get_terminal_width(environ: Mapping[str, str] | None = None) -> int:
    """Resolve terminal column width, honoring explicit COLUMNS override."""
    env = environ if environ is not None else os.environ
    raw_cols = env.get("COLUMNS", "").strip()
    if raw_cols:
        try:
            cols = int(raw_cols)
            if cols > 0:
                return cols
        except ValueError:
            pass
    return shutil.get_terminal_size(fallback=(80, 24)).columns


def should_use_color(
    *,
    is_tty: bool | None = None,
    environ: Mapping[str, str] | None = None,
) -> bool:
    """Determine whether terminal ANSI color should be emitted (respects NO_COLOR)."""
    env = environ if environ is not None else os.environ
    if env.get("NO_COLOR", "") != "":
        return False
    tty = sys.stdout.isatty() if is_tty is None else is_tty
    if not tty:
        return False
    term = env.get("TERM", "").lower()
    colorterm = env.get("COLORTERM", "").lower()
    if term == "dumb" and colorterm not in ("truecolor", "24bit"):
        return False
    return True


def supports_256_color(environ: Mapping[str, str] | None = None) -> bool:
    """Return True if the terminal advertises 256-color or truecolor support."""
    env = environ if environ is not None else os.environ
    colorterm = env.get("COLORTERM", "").lower()
    if colorterm in ("truecolor", "24bit"):
        return True
    term = env.get("TERM", "").lower()
    return any(
        hint in term
        for hint in ("256color", "truecolor", "kitty", "alacritty", "wezterm", "ghostty")
    )


def _style(text: str, *, use_color: bool, **kwargs: Any) -> str:
    if not use_color:
        return text
    return click.style(text, **kwargs)


def format_startup_logo(
    version: str,
    *,
    width: int | None = None,
    use_color: bool = True,
    use_256_color: bool = True,
) -> str:
    """Render the original Crewline pixel-art logo (or compact text on narrow terminals)."""
    cols = width if width is not None else get_terminal_width()

    c_top = 45 if use_256_color else "bright_cyan"
    c_mid = 39 if use_256_color else "cyan"
    c_bot = 33 if use_256_color else "blue"
    node_fg = 177 if use_256_color else "bright_magenta"
    edge_fg = 135 if use_256_color else "magenta"
    ver_fg = 117 if use_256_color else "cyan"
    sub_fg = 246 if use_256_color else "white"

    nodes = (
        _style("●", use_color=use_color, fg=node_fg, bold=True)
        + _style("─", use_color=use_color, fg=edge_fg)
        + _style("●", use_color=use_color, fg=node_fg, bold=True)
        + _style("─", use_color=use_color, fg=edge_fg)
        + _style("●", use_color=use_color, fg=node_fg, bold=True)
    )
    title = (
        _style("Crewline", use_color=use_color, bold=True)
        + " "
        + _style(version, use_color=use_color, fg=ver_fg)
    )
    tagline = _style(TAGLINE_ZH, use_color=use_color, fg=sub_fg, dim=not use_256_color)

    if cols < COMPACT_BANNER_WIDTH:
        return "\n".join([f"{nodes} {title}", tagline])

    lines = [
        f"    {_style('▄██████▄', use_color=use_color, fg=c_top)}",
        f"  {_style('▄██▀    ▀██', use_color=use_color, fg=c_top)}",
        f"  {_style('██', use_color=use_color, fg=c_mid)}    {nodes}     {title}",
        f"  {_style('▀██▄    ▄██', use_color=use_color, fg=c_bot)}     {tagline}",
        f"    {_style('▀██████▀', use_color=use_color, fg=c_bot)}",
    ]
    return "\n".join(lines)


def format_startup_banner(
    version: str,
    *,
    width: int | None = None,
    use_color: bool | None = None,
    is_tty: bool | None = None,
    environ: Mapping[str, str] | None = None,
) -> tuple[str, bool]:
    """Build the complete interactive startup banner (Logo + short help)."""
    env = environ if environ is not None else os.environ
    cols = width if width is not None else get_terminal_width(env)
    color_enabled = (
        should_use_color(is_tty=is_tty, environ=env)
        if use_color is None
        else use_color
    )
    is_256 = supports_256_color(env)

    logo = format_startup_logo(
        version,
        width=cols,
        use_color=color_enabled,
        use_256_color=is_256,
    )
    cmd_fg = 39 if is_256 else "cyan"
    hint_fg = 245 if is_256 else "white"

    if cols < COMPACT_BANNER_WIDTH:
        help_lines = [
            "",
            _style("常用命令:", use_color=color_enabled, bold=True),
            "  init / doctor / agent / task",
            "  run / logs / diff / review",
            _style(
                "运行 `crewline --help` 查看完整帮助。",
                use_color=color_enabled,
                fg=hint_fg,
                dim=not is_256,
            ),
        ]
    else:
        commands = [
            ("crewline init", "初始化本地工作区与默认 Agent"),
            ("crewline doctor", "检查各 Agent CLI / 凭证状态与能力"),
            ("crewline task create", "创建任务卡并分派 Agent"),
            ("crewline run <id>...", "并行运行任务卡（独立 Git worktree）"),
            ("crewline logs <id>", "查看脱敏后的运行日志"),
            ("crewline review approve", "人工审核通过（或 reject 退回重试）"),
        ]
        help_lines = ["", _style("常用命令:", use_color=color_enabled, bold=True)]
        for cmd, desc in commands:
            styled_cmd = _style(f"{cmd:<25}", use_color=color_enabled, fg=cmd_fg)
            help_lines.append(f"  {styled_cmd} {desc}")
        help_lines.extend(
            [
                "",
                _style(
                    "运行 `crewline --help` 查看全部命令与参数。",
                    use_color=color_enabled,
                    fg=hint_fg,
                    dim=not is_256,
                ),
            ]
        )

    banner = "\n".join(["", logo, *help_lines])
    return banner, color_enabled


def format_tasks_table(tasks: list[TaskCard], *, deleted: bool = False) -> str:
    if not tasks:
        if deleted:
            return "回收区暂无已删除的任务卡。"
        return "暂无任务卡。可使用 `crewline task create --title ...` 创建任务卡。"
    if deleted:
        lines = [
            f"{'ID':<10} {'原状态':<16} {'Agent':<12} {'删除时间':<25} 标题",
            "-" * 92,
        ]
        for task in tasks:
            status_str = f"{task.status.label_zh}({task.status.value})"
            agent_str = task.assigned_agent or "(未分派)"
            deleted_at = task.deleted_at or "-"
            lines.append(
                f"{task.id:<10} {status_str:<16} {agent_str:<12} {deleted_at:<25} {task.title}"
            )
        return "\n".join(lines)
    lines = [
        f"{'ID':<10} {'状态':<16} {'Agent':<12} {'分支':<20} 标题",
        "-" * 80,
    ]
    for task in tasks:
        status_str = f"{task.status.label_zh}({task.status.value})"
        agent_str = task.assigned_agent or "(未分派)"
        branch_str = task.worktree_branch or "-"
        lines.append(
            f"{task.id:<10} {status_str:<16} {agent_str:<12} {branch_str:<20} {task.title}"
        )
    return "\n".join(lines)


def format_task_detail(detail: TaskDetail) -> str:
    task = detail.task
    lines = [
        f"任务卡: {task.id}",
        f"标题: {task.title}",
        f"状态: {task.status.label_zh} ({task.status.value})",
    ]
    if task.is_deleted:
        lines.append(f"删除状态: 已删除（回收区，删除时间: {task.deleted_at}）")
    lines.extend(
        [
            f"分派 Agent: {task.assigned_agent or '(未分派)'}",
            f"目标仓库: {task.repo_path}",
            f"Worktree 路径: {task.worktree_path or '-'}",
            f"Worktree 分支: {task.worktree_branch or '-'}",
            f"基准提交: {task.worktree_base_commit[:12] if task.worktree_base_commit else '-'}",
            f"创建时间: {task.created_at}",
            f"更新时间: {task.updated_at}",
        ]
    )
    if task.description:
        lines.extend(["", "任务描述:", task.description])
    if task.last_review_comment:
        lines.extend(["", f"最新审核意见: {task.last_review_comment}"])
    if task.last_error:
        lines.extend(["", f"最近失败原因: {task.last_error}"])

    lines.append("")
    lines.append(f"运行记录 ({len(detail.runs)}):")
    if not detail.runs:
        lines.append("  （暂无运行记录）")
    else:
        for run in detail.runs:
            files_str = ", ".join(run.changed_files) if run.changed_files else "-"
            lines.append(
                f"  - {run.id} | {run.status.value} | agent={run.agent_name} "
                f"({run.provider}/{run.model}) | exit={run.exit_code} | "
                f"started={run.started_at}"
            )
            if run.summary:
                lines.append(f"    摘要: {run.summary}")
            if run.error_message:
                lines.append(f"    错误: {run.error_message}")
            lines.append(f"    变更文件: {files_str}")
            lines.append(f"    日志: {run.log_path}")

    lines.append("")
    lines.append(f"审核记录 ({len(detail.reviews)}):")
    if not detail.reviews:
        lines.append("  （暂无审核记录）")
    else:
        for rev in detail.reviews:
            decision_zh = "通过" if rev.decision.value == "approved" else "退回待办"
            lines.append(
                f"  - {rev.id} | {decision_zh} ({rev.decision.value}) | "
                f"run={rev.run_id or '-'} | {rev.created_at} | 意见: {rev.comment}"
            )
    return "\n".join(lines)


def format_agents_table(
    agents: list[AgentConfig],
    capabilities_map: dict[str, list[str]],
) -> str:
    if not agents:
        return "暂无已配置的 Agent。"
    lines = [
        f"{'名称':<14} {'厂商':<12} {'模型':<20} {'认证方式':<14} {'凭证引用':<18} 能力",
        "-" * 106,
    ]
    for agent in agents:
        model_str = agent.model or "(CLI 默认)"
        if agent.auth_method.value == "api_key":
            cred = f"env:{agent.api_key_env}"
        elif agent.auth_method.value == "official_cli":
            cred = f"cli:{agent.cli_bin or agent.provider.value}"
        else:
            cred = "无需账号"
        caps = "、".join(capabilities_map.get(agent.name, []))
        lines.append(
            f"{agent.name:<14} {agent.provider.value:<12} {model_str:<20} "
            f"{agent.auth_method.value:<14} {cred:<18} {caps}"
        )
    return "\n".join(lines)


def format_doctor_report(items: list[DoctorItem]) -> str:
    if not items:
        return "未配置任何 Agent。"
    ready_count = sum(1 for item in items if item.ready)
    lines = [
        f"Crewline Doctor 检查结果（就绪 {ready_count}/{len(items)}）",
        "=" * 76,
    ]
    for item in items:
        icon = "[OK]" if item.ready else "[MISSING]"
        caps = "、".join(capability_labels(item.capabilities))
        lines.append(
            f"{icon} {item.agent_name} | 厂商: {item.provider} | 模型: {item.model} | 认证: {item.auth_method}"
        )
        lines.append(f"    能力: {caps}")
        if item.credential_ref:
            lines.append(f"    凭证引用: {item.credential_ref}")
        lines.append(f"    状态: {item.status_label} - {item.summary}")
        if item.missing_items:
            lines.append(f"    缺少项: {'；'.join(item.missing_items)}")
        if item.setup_steps:
            lines.append("    配置步骤:")
            for idx, step in enumerate(item.setup_steps, start=1):
                lines.append(f"      {idx}. {step}")
        lines.append("")
    return "\n".join(lines).rstrip()


def format_run_outcomes(outcomes: list[RunOutcome]) -> str:
    lines: list[str] = []
    for item in outcomes:
        if not item.started:
            lines.append(f"[BLOCKED] {item.task_id}: 未启动 - {item.error}")
            continue
        assert item.task is not None and item.run is not None
        if item.succeeded:
            lines.append(
                f"[OK] {item.task_id} ({item.run.id}) -> {item.task.status.label_zh}({item.task.status.value})"
                f" | branch={item.run.worktree_branch or '-'} | {item.run.summary}"
            )
        else:
            lines.append(
                f"[FAILED] {item.task_id} ({item.run.id}) -> {item.task.status.label_zh}({item.task.status.value})"
                f" | 原因: {item.error}"
            )
            lines.append(f"         日志: {item.run.log_path}")
    return "\n".join(lines)
