"""Click-based CLI entry point for Crewline v0.1."""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path
from typing import Any

import click

from crewline import __version__
from crewline.cli.formatters import (
    format_agents_table,
    format_doctor_report,
    format_run_outcomes,
    format_startup_banner,
    format_task_detail,
    format_tasks_table,
    to_json,
)
from crewline.domain.capabilities import capability_labels
from crewline.domain.errors import CrewlineError
from crewline.domain.models import AuthMethod, ProviderId, RunStatus, TaskStatus
from crewline.engine.service import CrewlineService
from crewline.workspace.terminal_sessions import TerminalSessions


def _is_interactive_stdout() -> bool:
    """Return True when stdout is attached to an interactive terminal."""
    return bool(getattr(sys.stdout, "isatty", lambda: False)())


def _can_open_workbench() -> bool:
    return (
        sys.stdin.isatty()
        and _is_interactive_stdout()
        and os.environ.get("TERM") not in (None, "", "dumb")
    )


def _get_service(ctx: click.Context) -> CrewlineService:
    return ctx.obj["service"]


def _handle_error(exc: CrewlineError) -> None:
    click.echo(f"错误: {exc}", err=True)
    raise SystemExit(1)


@click.group(
    invoke_without_command=True,
    context_settings={"help_option_names": ["-h", "--help"]},
)
@click.version_option(version=__version__, prog_name="crewline")
@click.option(
    "--home",
    "home_dir",
    envvar="CREWLINE_HOME",
    type=click.Path(path_type=Path),
    default=None,
    help="Crewline 状态目录（默认为当前目录下的 .crewline）。",
)
@click.pass_context
def cli(ctx: click.Context, home_dir: Path | None) -> None:
    """Crewline v0.1 - 本地多 Agent 任务管理 CLI。"""
    resolved_home = home_dir if home_dir is not None else (Path.cwd() / ".crewline")
    ctx.ensure_object(dict)
    ctx.obj["service"] = CrewlineService(home_dir=resolved_home)
    if ctx.invoked_subcommand is None:
        if _can_open_workbench():
            from crewline.cli.workbench import open_workbench

            open_workbench(_get_service(ctx))
        elif _is_interactive_stdout():
            banner, use_color = format_startup_banner(__version__, is_tty=True)
            click.echo(banner, color=use_color)
        else:
            click.echo(ctx.get_help())


@cli.command("_workbench-run", hidden=True)
@click.argument("task_id")
@click.argument("launch_id")
@click.pass_context
def _cmd_workbench_run(ctx: click.Context, task_id: str, launch_id: str) -> None:
    outcome = _get_service(ctx).run_workbench_task(task_id, launch_id)
    click.echo(format_run_outcomes([outcome]))
    if not outcome.succeeded:
        ctx.exit(1)


@cli.group("session")
def cmd_session() -> None:
    """在脚本中启动、查看和重连工作台会话。"""


@cmd_session.command("list")
@click.option("--json", "as_json", is_flag=True, help="以 JSON 格式输出。")
@click.pass_context
def cmd_session_list(ctx: click.Context, as_json: bool) -> None:
    try:
        active, _ = TerminalSessions(_get_service(ctx)).refresh()
    except CrewlineError as exc:
        _handle_error(exc)
        return
    if as_json:
        click.echo(to_json([run.to_dict() for run in active.values()]))
    else:
        rows = [f"{run.task_id}\t{run.agent_name}\t{run.id}" for run in active.values()]
        click.echo("\n".join(rows) or "当前没有活跃的后台会话。")


@cmd_session.command("start")
@click.argument("task_id")
@click.pass_context
def cmd_session_start(ctx: click.Context, task_id: str) -> None:
    """将已分派的待办任务启动为独立后台会话。"""
    try:
        run = TerminalSessions(_get_service(ctx)).start(task_id)
    except CrewlineError as exc:
        _handle_error(exc)
        return
    click.echo(f"已启动 {task_id}（{run.id}）。")


@cmd_session.command("attach")
@click.argument("task_id")
@click.pass_context
def cmd_session_attach(ctx: click.Context, task_id: str) -> None:
    """连接任务终端。按 Ctrl+b d 断开，任务继续运行。"""
    service = _get_service(ctx)
    try:
        task = service.get_task_detail(task_id).task
        TerminalSessions(service).attach(task.id)
    except CrewlineError as exc:
        _handle_error(exc)


@cmd_session.command("wait")
@click.argument("task_id")
@click.option("--timeout", type=click.FloatRange(min=0, min_open=True), default=300.0)
@click.option("--json", "as_json", is_flag=True, help="以 JSON 格式输出。")
@click.pass_context
def cmd_session_wait(ctx: click.Context, task_id: str, timeout: float, as_json: bool) -> None:
    """等待任务当前的运行记录结束。"""
    service = _get_service(ctx)
    sessions = TerminalSessions(service)
    try:
        detail = service.get_task_detail(task_id)
        if not detail.task.last_run_id:
            raise CrewlineError(f"任务 {task_id} 还没有运行记录。")
        run = next(item for item in detail.runs if item.id == detail.task.last_run_id)
        deadline = time.monotonic() + timeout
        while run.status == RunStatus.RUNNING:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise CrewlineError(f"等待 {run.id} 超时；会话可能仍在运行。")
            managed = any(item.id == run.id for item in service.list_active_workbench_runs())
            if managed and sessions.is_alive(task_id) is False:
                service.recover_workbench_run(run)
            run = next(item for item in service.get_task_detail(task_id).runs if item.id == run.id)
            if run.status == RunStatus.RUNNING:
                time.sleep(min(0.2, remaining))
    except CrewlineError as exc:
        _handle_error(exc)
        return
    click.echo(to_json(run.to_dict()) if as_json else f"{task_id} {run.id}: {run.status.value}")
    if run.status != RunStatus.SUCCEEDED:
        ctx.exit(1)


@cli.command("init")
@click.option("--force", is_flag=True, help="覆盖已有的 config.toml 并重新生成默认 Agent。")
@click.pass_context
def cmd_init(ctx: click.Context, force: bool) -> None:
    """初始化本地 .crewline 工作区、默认 Agent 配置和 SQLite 数据库。"""
    service = _get_service(ctx)
    try:
        home_dir, created = service.init_workspace(force=force)
    except CrewlineError as exc:
        _handle_error(exc)
        return
    status = "已创建默认 config.toml" if created else "保留已有 config.toml"
    click.echo(f"Crewline 已初始化：{home_dir}（{status}）")
    click.echo("提示：请将 `.crewline/` 加入仓库的 `.gitignore`，然后运行 `crewline doctor` 检查 Agent 状态。")


@cli.command("doctor")
@click.option("--agent", "agent_name", default=None, help="仅检查指定名称的 Agent。")
@click.option("--json", "as_json", is_flag=True, help="以 JSON 格式输出结果。")
@click.pass_context
def cmd_doctor(ctx: click.Context, agent_name: str | None, as_json: bool) -> None:
    """检查各 Agent 所需的官方 CLI / 登录状态 / 凭证环境变量，并显示能力与配置步骤。"""
    service = _get_service(ctx)
    try:
        report = service.doctor(agent_name=agent_name)
    except CrewlineError as exc:
        _handle_error(exc)
        return
    if as_json:
        click.echo(to_json([item.to_dict() for item in report]))
    else:
        click.echo(format_doctor_report(report))


# --- Agent management commands ---


@cli.group("agent")
def cmd_agent() -> None:
    """管理具名 Agent 配置（只保存凭证引用，不保存密钥）。"""


@cmd_agent.command("list")
@click.option("--json", "as_json", is_flag=True, help="以 JSON 格式输出。")
@click.pass_context
def cmd_agent_list(ctx: click.Context, as_json: bool) -> None:
    """列出所有已配置的具名 Agent 及其能力。"""
    service = _get_service(ctx)
    try:
        agents = service.list_agents()
    except CrewlineError as exc:
        _handle_error(exc)
        return
    caps_map = {
        a.name: capability_labels(service.registry.get(a.provider).spec.capabilities)
        for a in agents
    }
    if as_json:
        payload = []
        for a in agents:
            item = a.to_dict()
            spec = service.registry.get(a.provider).spec
            item["capabilities"] = [c.value for c in spec.capabilities]
            item["capabilities_zh"] = capability_labels(spec.capabilities)
            payload.append(item)
        click.echo(to_json(payload))
    else:
        click.echo(format_agents_table(agents, caps_map))


@cmd_agent.command("show")
@click.argument("name")
@click.option("--json", "as_json", is_flag=True, help="以 JSON 格式输出。")
@click.pass_context
def cmd_agent_show(ctx: click.Context, name: str, as_json: bool) -> None:
    """查看单个 Agent 的详细配置与能力。"""
    service = _get_service(ctx)
    try:
        agent = service.get_agent(name)
        spec = service.registry.get(agent.provider).spec
    except CrewlineError as exc:
        _handle_error(exc)
        return
    if as_json:
        data = agent.to_dict()
        data["capabilities"] = [c.value for c in spec.capabilities]
        data["capabilities_zh"] = capability_labels(spec.capabilities)
        click.echo(to_json(data))
        return
    click.echo(f"名称: {agent.name}")
    click.echo(f"厂商: {agent.provider.value} ({spec.display_name})")
    click.echo(f"模型: {agent.model or '(CLI 默认)'}")
    click.echo(f"认证方式: {agent.auth_method.value}")
    if agent.api_key_env:
        click.echo(f"凭证环境变量引用: {agent.api_key_env}")
    if agent.cli_bin:
        click.echo(f"CLI 命令: {agent.cli_bin}")
    if agent.base_url:
        click.echo(f"API Base URL: {agent.base_url}")
    click.echo(f"能力: {'、'.join(capability_labels(spec.capabilities))}")
    if agent.description:
        click.echo(f"说明: {agent.description}")


@cmd_agent.command("add")
@click.argument("name")
@click.option(
    "--provider",
    type=click.Choice([p.value for p in ProviderId], case_sensitive=False),
    required=True,
    help="厂商适配器类型。",
)
@click.option("--model", default=None, help="模型名称（Codex 可留空以使用 CLI 默认模型）。")
@click.option(
    "--auth-method",
    type=click.Choice([a.value for a in AuthMethod], case_sensitive=False),
    default=None,
    help="官方支持的认证方式。",
)
@click.option(
    "--api-key-env",
    default=None,
    help="API Key 环境变量名（例如 GEMINI_API_KEY，严禁填写密钥值）。",
)
@click.option("--cli-bin", default=None, help="官方 CLI 可执行文件名或路径。")
@click.option("--base-url", default=None, help="可选 API Base URL。")
@click.option(
    "--extra-arg",
    "extra_args",
    multiple=True,
    help="透传给官方 CLI 的附加参数（可重复指定，例如 --extra-arg=-s --extra-arg=danger-full-access）。",
)
@click.option("--description", default="", help="Agent 备注说明。")
@click.option("--overwrite", is_flag=True, help="如同名 Agent 已存在则覆盖。")
@click.pass_context
def cmd_agent_add(
    ctx: click.Context,
    name: str,
    provider: str,
    model: str | None,
    auth_method: str | None,
    api_key_env: str | None,
    cli_bin: str | None,
    base_url: str | None,
    extra_args: tuple[str, ...],
    description: str,
    overwrite: bool,
) -> None:
    """添加或更新具名 Agent 配置。"""
    service = _get_service(ctx)
    try:
        agent = service.add_agent(
            name=name,
            provider=ProviderId(provider),
            model=model,
            auth_method=AuthMethod(auth_method) if auth_method else None,
            api_key_env=api_key_env,
            cli_bin=cli_bin,
            base_url=base_url,
            extra_args=list(extra_args),
            description=description,
            overwrite=overwrite,
        )
    except CrewlineError as exc:
        _handle_error(exc)
        return
    click.echo(
        f"已保存 Agent `{agent.name}`（provider={agent.provider.value}, "
        f"model={agent.model or '(CLI 默认)'}, auth={agent.auth_method.value}）。"
    )


@cmd_agent.command("remove")
@click.argument("name")
@click.pass_context
def cmd_agent_remove(ctx: click.Context, name: str) -> None:
    """删除具名 Agent 配置。"""
    service = _get_service(ctx)
    try:
        service.remove_agent(name)
    except CrewlineError as exc:
        _handle_error(exc)
        return
    click.echo(f"已删除 Agent `{name}`。")


# --- Task card commands ---


@cli.group("task")
def cmd_task() -> None:
    """管理任务卡（建卡、查看、分派）。"""


@cmd_task.command("create")
@click.option("--title", required=True, help="任务卡标题。")
@click.option("--description", "-d", default="", help="任务卡详细描述。")
@click.option("--agent", "agent_name", default=None, help="可选：创建时直接分派给指定 Agent。")
@click.option(
    "--repo",
    "repo_path",
    type=click.Path(path_type=Path),
    default=None,
    help="目标 Git 仓库或工作目录（默认当前目录）。",
)
@click.option("--json", "as_json", is_flag=True, help="以 JSON 格式输出。")
@click.pass_context
def cmd_task_create(
    ctx: click.Context,
    title: str,
    description: str,
    agent_name: str | None,
    repo_path: Path | None,
    as_json: bool,
) -> None:
    """创建一张处于「待办」状态的任务卡。"""
    service = _get_service(ctx)
    try:
        task = service.create_task(
            title=title,
            description=description,
            repo_path=repo_path,
            assigned_agent=agent_name,
        )
    except CrewlineError as exc:
        _handle_error(exc)
        return
    if as_json:
        click.echo(to_json(task.to_dict()))
    else:
        click.echo(
            f"已创建任务卡 {task.id} | 状态: {task.status.label_zh}({task.status.value}) | "
            f"Agent: {task.assigned_agent or '(未分派)'} | 标题: {task.title}"
        )


@cmd_task.command("list")
@click.option(
    "--status",
    type=click.Choice([s.value for s in TaskStatus], case_sensitive=False),
    default=None,
    help="按状态过滤（todo / in_progress / in_review / done）。",
)
@click.option("--deleted", is_flag=True, help="仅查看回收区中已删除的任务卡。")
@click.option("--json", "as_json", is_flag=True, help="以 JSON 格式输出。")
@click.pass_context
def cmd_task_list(
    ctx: click.Context,
    status: str | None,
    deleted: bool,
    as_json: bool,
) -> None:
    """列出任务卡（默认隐藏已删除卡，传入 --deleted 查看回收区）。"""
    service = _get_service(ctx)
    try:
        tasks = service.list_tasks(
            status=TaskStatus(status) if status else None,
            deleted=deleted,
        )
    except CrewlineError as exc:
        _handle_error(exc)
        return
    if as_json:
        click.echo(to_json([t.to_dict() for t in tasks]))
    else:
        click.echo(format_tasks_table(tasks, deleted=deleted))


@cmd_task.command("show")
@click.argument("task_id")
@click.option("--json", "as_json", is_flag=True, help="以 JSON 格式输出。")
@click.pass_context
def cmd_task_show(ctx: click.Context, task_id: str, as_json: bool) -> None:
    """查看任务卡详情、运行记录与审核历史。"""
    service = _get_service(ctx)
    try:
        detail = service.get_task_detail(task_id)
    except CrewlineError as exc:
        _handle_error(exc)
        return
    if as_json:
        payload: dict[str, Any] = {
            "task": detail.task.to_dict(),
            "runs": [r.to_dict() for r in detail.runs],
            "reviews": [rv.to_dict() for rv in detail.reviews],
        }
        click.echo(to_json(payload))
    else:
        click.echo(format_task_detail(detail))


@cmd_task.command("assign")
@click.argument("task_id")
@click.argument("agent_name")
@click.pass_context
def cmd_task_assign(ctx: click.Context, task_id: str, agent_name: str) -> None:
    """将任务卡分派给指定名称的 Agent。"""
    service = _get_service(ctx)
    try:
        task = service.assign_task(task_id, agent_name)
    except CrewlineError as exc:
        _handle_error(exc)
        return
    click.echo(f"已将任务卡 {task.id} 分派给 Agent `{task.assigned_agent}`。")


@cmd_task.command("delete")
@click.argument("task_id")
@click.option("--json", "as_json", is_flag=True, help="以 JSON 格式输出。")
@click.pass_context
def cmd_task_delete(ctx: click.Context, task_id: str, as_json: bool) -> None:
    """可恢复删除任务卡（移入回收区，保留日志、审核、run 记录与 Git worktree/分支）。"""
    service = _get_service(ctx)
    try:
        task = service.delete_task(task_id)
    except CrewlineError as exc:
        _handle_error(exc)
        return
    if as_json:
        click.echo(to_json(task.to_dict()))
    else:
        click.echo(
            f"已将任务卡 {task.id} 移入回收区（原状态: {task.status.label_zh}({task.status.value})；"
            f"日志与 worktree 已保留，可运行 `crewline task restore {task.id}` 恢复）。"
        )


@cmd_task.command("restore")
@click.argument("task_id")
@click.option("--json", "as_json", is_flag=True, help="以 JSON 格式输出。")
@click.pass_context
def cmd_task_restore(ctx: click.Context, task_id: str, as_json: bool) -> None:
    """从回收区恢复任务卡（保留原 ID、原状态与原分派）。"""
    service = _get_service(ctx)
    try:
        task = service.restore_task(task_id)
    except CrewlineError as exc:
        _handle_error(exc)
        return
    if as_json:
        click.echo(to_json(task.to_dict()))
    else:
        click.echo(
            f"已恢复任务卡 {task.id} | 状态: {task.status.label_zh}({task.status.value}) | "
            f"Agent: {task.assigned_agent or '(未分派)'}"
        )


# --- Run, logs, diff, review commands ---


@cli.command("run")
@click.argument("task_ids", nargs=-1)
@click.option("--all-todo", is_flag=True, help="并行运行所有已分派 Agent 的待办任务卡。")
@click.option("--max-workers", type=int, default=4, show_default=True, help="最大并行数。")
@click.option("--timeout", "timeout_seconds", type=int, default=None, help="单次运行超时时间（秒）。")
@click.option("--json", "as_json", is_flag=True, help="以 JSON 格式输出运行结果。")
@click.pass_context
def cmd_run(
    ctx: click.Context,
    task_ids: tuple[str, ...],
    all_todo: bool,
    max_workers: int,
    timeout_seconds: int | None,
    as_json: bool,
) -> None:
    """运行一张或多张任务卡（多张卡自动并行，同 Git 仓库每张卡使用独立 worktree）。"""
    service = _get_service(ctx)
    try:
        if all_todo:
            outcomes = service.run_all_todo(
                max_workers=max_workers, timeout_seconds=timeout_seconds
            )
        else:
            outcomes = service.run_tasks(
                list(task_ids),
                max_workers=max_workers,
                timeout_seconds=timeout_seconds,
            )
    except CrewlineError as exc:
        _handle_error(exc)
        return

    if as_json:
        payload = [
            {
                "task_id": o.task_id,
                "started": o.started,
                "succeeded": o.succeeded,
                "error": o.error,
                "task": o.task.to_dict() if o.task else None,
                "run": o.run.to_dict() if o.run else None,
            }
            for o in outcomes
        ]
        click.echo(to_json(payload))
    else:
        click.echo(format_run_outcomes(outcomes))

    if any(not o.succeeded for o in outcomes):
        raise SystemExit(1)


@cli.command("logs")
@click.argument("task_id")
@click.option("--run", "run_id", default=None, help="指定运行记录 ID（默认查看最近一次）。")
@click.option("--tail", type=int, default=None, help="仅显示最后 N 行。")
@click.pass_context
def cmd_logs(
    ctx: click.Context,
    task_id: str,
    run_id: str | None,
    tail: int | None,
) -> None:
    """查看任务卡的运行日志（敏感凭证已自动脱敏）。"""
    service = _get_service(ctx)
    try:
        run, content = service.read_logs(task_id=task_id, run_id=run_id, tail=tail)
    except CrewlineError as exc:
        _handle_error(exc)
        return
    click.echo(
        f"--- 任务卡 {task_id} | 运行记录 {run.id} | 状态: {run.status.value} | 日志文件: {run.log_path} ---"
    )
    click.echo(content)


@cli.command("diff")
@click.argument("task_id")
@click.pass_context
def cmd_diff(ctx: click.Context, task_id: str) -> None:
    """查看任务卡独立 Git worktree 中的文件变更。"""
    service = _get_service(ctx)
    try:
        diff_text = service.get_diff(task_id)
    except CrewlineError as exc:
        _handle_error(exc)
        return
    click.echo(diff_text)


@cli.group("review")
def cmd_review() -> None:
    """人工审核待审核（in_review）的任务卡。"""


@cmd_review.command("approve")
@click.argument("task_id")
@click.option("--comment", "-c", default="", help="可选的审核通过备注。")
@click.option(
    "--cleanup-worktree",
    is_flag=True,
    help="审核通过前安全清理干净的 worktree 目录（非强制删除，脏 worktree 将拒绝清理并保留待审核状态与结果分支）。",
)
@click.pass_context
def cmd_review_approve(
    ctx: click.Context,
    task_id: str,
    comment: str,
    cleanup_worktree: bool,
) -> None:
    """审核通过，将任务卡从「待审核」流转为「审核完成」。"""
    service = _get_service(ctx)
    try:
        task, review = service.approve_task(
            task_id=task_id,
            comment=comment,
            cleanup_worktree=cleanup_worktree,
        )
    except CrewlineError as exc:
        _handle_error(exc)
        return
    click.echo(
        f"任务卡 {task.id} 审核通过（{review.id}）-> {task.status.label_zh}({task.status.value})"
        f" | 意见: {review.comment}"
    )


@cmd_review.command("reject")
@click.argument("task_id")
@click.option("--comment", "-c", required=True, help="退回意见（下次重试时会自动传给 Agent）。")
@click.pass_context
def cmd_review_reject(ctx: click.Context, task_id: str, comment: str) -> None:
    """附带意见退回，将任务卡从「待审核」退回「待办」。"""
    service = _get_service(ctx)
    try:
        task, review = service.reject_task(task_id=task_id, comment=comment)
    except CrewlineError as exc:
        _handle_error(exc)
        return
    click.echo(
        f"任务卡 {task.id} 已退回（{review.id}）-> {task.status.label_zh}({task.status.value})"
        f" | 意见: {review.comment}"
    )
