"""Interactive task board and launcher for persistent Agent sessions."""

from __future__ import annotations

import curses
import unicodedata
from contextlib import contextmanager
from typing import Iterator

import click

from crewline import __version__
from crewline.cli.formatters import format_startup_logo, should_use_color
from crewline.domain.errors import CrewlineError
from crewline.domain.models import AuthMethod, ProviderId, TaskCard, TaskStatus
from crewline.engine.service import CrewlineService
from crewline.runtime import clean_os_environ
from crewline.workspace.terminal_sessions import TerminalSessions

_HELP_ACTIVE = (
    "↑/↓ 选择  Enter 进入会话  n 建卡  a 指派  s 开始  x 删除",
    "l 日志  d 变更  p 审核通过  r 退回  b 回收区  q 退出工作台",
)
_HELP_DELETED = (
    "回收区（只读，不可启动）：↑/↓ 选择  u 恢复  l 日志  d 变更",
    "b 返回当前卡片  q 退出工作台",
)
_INTERACTIVE_PROVIDERS = {ProviderId.CODEX, ProviderId.CLAUDE_CODE, ProviderId.GROK_BUILD}


def _clip(text: str, width: int) -> str:
    cells = 0
    visible: list[str] = []
    for char in text:
        if not char.isprintable():
            char = " "
        size = 2 if unicodedata.east_asian_width(char) in {"W", "F"} else 1
        if unicodedata.combining(char):
            size = 0
        if cells + size > width:
            break
        visible.append(char)
        cells += size
    return "".join(visible)


@contextmanager
def _terminal() -> Iterator[None]:
    """Temporarily return stdin/stdout to a pager, prompt, or tmux client."""
    curses.def_prog_mode()
    curses.endwin()
    try:
        yield
    finally:
        curses.reset_prog_mode()


class Workbench:
    def __init__(self, service: CrewlineService) -> None:
        self.service = service
        self.sessions = TerminalSessions(service)
        self.selected_id: str | None = None
        self.show_deleted: bool = False
        self.message = ""

    def draw(self, screen: curses.window) -> TaskCard | None:
        screen.erase()
        rows, cols = screen.getmaxyx()
        accent = curses.color_pair(1) if curses.has_colors() and should_use_color(is_tty=True) else 0

        def put(y: int, x: int, text: str, attr: int = 0, limit: int | None = None) -> None:
            if 0 <= y < rows and 0 <= x < cols - 1:
                available = cols - x - 1
                screen.addstr(y, x, _clip(text, min(available, limit) if limit is not None else available), attr)

        logo = format_startup_logo(
            __version__, width=cols if rows >= 17 else 0, use_color=False
        ).splitlines()
        for y, line in enumerate(logo):
            put(y, 0, line, accent | curses.A_BOLD)
        top = len(logo)
        put(top, 1, str(self.service.home_dir.parent), curses.A_DIM)
        if not self.service.config_path.is_file():
            put(top + 2, 1, "按 i 初始化工作区并开始建卡，按 q 退出。", curses.A_BOLD)
            put(rows - 1, 1, self.message)
            screen.refresh()
            return None

        active, recovered = self.sessions.refresh()
        if recovered:
            self.message = f"后台会话已中断，任务退回待办：{', '.join(recovered)}"
        active_tasks = self.service.list_tasks(deleted=False)
        deleted_tasks = self.service.list_tasks(deleted=True)
        tasks = deleted_tasks if self.show_deleted else active_tasks
        agents = self.service.list_agents()

        if self.show_deleted:
            header = f"回收区（已删除 {len(deleted_tasks)} 张 · 仅支持查看与恢复）"
        else:
            counts = {status: sum(task.status == status for task in active_tasks) for status in TaskStatus}
            header = "   ".join(f"{status.label_zh} {count}" for status, count in counts.items())
            if deleted_tasks:
                header += f"   · 回收区 {len(deleted_tasks)}"
        put(top + 1, 1, header, curses.A_BOLD)

        selected = next((task for task in tasks if task.id == self.selected_id), None)
        if selected is None and tasks:
            selected = tasks[0]
            self.selected_id = selected.id
        elif selected is None:
            self.selected_id = None

        body_top = top + 3
        body_height = max(0, rows - body_top - 4)
        split = cols >= 88
        task_width = cols // 2 - 2 if split else cols - 2
        if tasks and selected is not None:
            offset = max(0, tasks.index(selected) - body_height + 1)
            for index, task in enumerate(tasks[offset:offset + body_height]):
                run = active.get(task.id)
                assigned = run.agent_name if run else task.assigned_agent or "未指派"
                prefix = "[已删除]" if self.show_deleted else ""
                item = f"{task.id} {prefix}{task.status.label_zh}  {assigned}  {task.title}"
                style = curses.A_REVERSE if task.id == self.selected_id else 0
                put(body_top + index, 1, item, style, task_width)
        else:
            empty_hint = "回收区为空。按 b 返回当前卡片。" if self.show_deleted else "按 n 创建第一张任务卡。"
            put(body_top, 1, empty_hint, limit=task_width)

        if split:
            x = cols // 2 + 1
            if selected:
                put(body_top, x, selected.title, curses.A_BOLD)
                if self.show_deleted:
                    label = f"已删除（原状态：{selected.status.label_zh}） · 按 u 恢复"
                    detail_text = f"删除时间: {selected.deleted_at or '-'}  {selected.description}"
                else:
                    run = active.get(selected.id)
                    if run:
                        label = (
                            "交互会话 · Enter 进入"
                            if run.provider in _INTERACTIVE_PROVIDERS
                            else "后台执行 · Enter 查看"
                        )
                    else:
                        label = (
                            "运行中（外部命令）"
                            if selected.status == TaskStatus.IN_PROGRESS
                            else selected.status.label_zh
                        )
                    detail_text = selected.last_error or selected.last_review_comment or selected.description
                put(body_top + 1, x, label, accent)
                put(body_top + 2, x, detail_text)
            put(body_top + 4, x, "已配置的 Agent", curses.A_BOLD)
            for index, agent in enumerate(agents[:max(0, body_height - 5)]):
                running = sum(run.agent_name == agent.name for run in active.values())
                capability = "交互" if agent.auth_method == AuthMethod.OFFICIAL_CLI else "后台"
                put(body_top + 5 + index, x, f"{agent.name}  {agent.provider}  {capability}  · 运行 {running}")

        message = self.message
        if not message and not self.show_deleted and not self.sessions.tmux:
            message = "tmux 未安装。可以先管理卡片，安装后即可启动会话。"
        put(rows - 4, 1, message)
        help_lines = _HELP_DELETED if self.show_deleted else _HELP_ACTIVE
        for index, line in enumerate(help_lines):
            put(rows - 3 + index, 1, line, curses.A_DIM)
        screen.refresh()
        return selected

    def _prompt(self, label: str) -> str:
        with _terminal():
            return input(f"\n{label} ").strip()

    def _prompt_agent(self, label: str) -> str:
        agents = self.service.list_agents()
        with _terminal():
            for agent in agents:
                click.echo(f"  {agent.name}  ({agent.provider})")
            return input(f"\n{label} ").strip()

    def handle(self, key: str | int, task: TaskCard | None) -> None:
        if not self.service.config_path.is_file():
            if key == "i":
                self.service.init_workspace()
                self.message = "工作区已初始化。"
            return

        if key == "b":
            self.show_deleted = not self.show_deleted
            self.selected_id = None
            self.message = (
                "已切换到回收区（按 u 恢复，按 b 返回当前卡片）。"
                if self.show_deleted
                else "已返回当前卡片列表。"
            )
            return

        if key in (curses.KEY_UP, curses.KEY_DOWN, "k", "j") and task:
            tasks = self.service.list_tasks(deleted=self.show_deleted)
            if not tasks:
                self.selected_id = None
                return
            index = next((i for i, item in enumerate(tasks) if item.id == task.id), 0)
            step = -1 if key in (curses.KEY_UP, "k") else 1
            self.selected_id = tasks[max(0, min(index + step, len(tasks) - 1))].id
            return

        if task and key in ("l", "d"):
            contents = self.service.read_logs(task.id)[1] if key == "l" else self.service.get_diff(task.id)
            with _terminal(), clean_os_environ():
                click.echo_via_pager(contents)
            return

        if self.show_deleted:
            if key == "u" and task:
                restored = self.service.restore_task(task.id)
                self.selected_id = None
                self.message = f"已恢复 {restored.id}（原状态：{restored.status.label_zh}）。"
            elif key in ("s", "\n", "\r", curses.KEY_ENTER, "a", "p", "r", "x", "n"):
                self.message = "回收区仅支持查看和恢复；请先按 u 恢复或按 b 返回当前卡片。"
            return

        if key == "n":
            title = self._prompt("任务标题（空白取消）")
            if title:
                description = self._prompt("任务描述")
                agent_name = self._prompt_agent("指派 Agent（回车跳过）") or None
                card = self.service.create_task(title, description, assigned_agent=agent_name)
                self.selected_id = card.id
                if card.assigned_agent:
                    self.message = f"已创建 {card.id} 并指派给 {card.assigned_agent}。按 s 启动或 a 改派。"
                else:
                    self.message = f"已创建 {card.id}。按 a 指派 Agent。"
        elif task and key == "a":
            name = self._prompt_agent("Agent 名称（空白取消）")
            if name:
                self.service.assign_task(task.id, name)
                self.message = f"{task.id} 已指派给 {name}。"
        elif task and key == "x":
            if self._prompt(f"确认删除任务卡 {task.id}（移入回收区，可恢复）？[y/N]").lower() == "y":
                self.service.delete_task(task.id)
                self.selected_id = None
                self.message = f"{task.id} 已移入回收区（按 b 查看，按 u 恢复）。"
            else:
                self.message = f"已取消删除 {task.id}。"
        elif task and key == "s":
            self.sessions.start(task.id)
            self.message = f"已启动 {task.id}。按 Enter 进入，Ctrl+b d 返回工作台。"
        elif task and key in ("\n", "\r", curses.KEY_ENTER):
            if self.sessions.is_alive(task.id) is not True:
                self.message = "该卡当前没有可连接的会话。按 l 查看日志。"
                return
            with _terminal():
                self.sessions.attach(task.id)
        elif task and key in ("p", "r"):
            if task.status != TaskStatus.IN_REVIEW:
                self.message = "请选择待审核的任务。"
                return
            if key == "p" and self._prompt("通过审核？[y/N]").lower() == "y":
                self.service.approve_task(task.id)
                self.message = f"{task.id} 已审核完成。"
            if key == "r":
                comment = self._prompt("退回意见（空白取消）")
                if comment:
                    self.service.reject_task(task.id, comment)
                    self.message = f"{task.id} 已退回待办。"
        elif key == "u":
            self.message = "当前不在回收区，按 b 切换到回收区后可按 u 恢复卡片。"

    def run(self, screen: curses.window) -> None:
        try:
            curses.curs_set(0)
        except curses.error:
            pass
        if curses.has_colors() and should_use_color(is_tty=True):
            try:
                curses.use_default_colors()
                curses.init_pair(1, curses.COLOR_CYAN, -1)
            except curses.error:
                curses.init_pair(1, curses.COLOR_CYAN, curses.COLOR_BLACK)
        screen.timeout(750)
        while True:
            try:
                task = self.draw(screen)
            except CrewlineError as exc:
                # Leave the workbench cleanly if state cannot be loaded.
                raise click.ClickException(str(exc)) from exc
            try:
                key = screen.get_wch()
            except curses.error:
                continue
            if key in ("q", "Q"):
                return
            try:
                self.handle(key, task)
            except CrewlineError as exc:
                self.message = str(exc)
            except (EOFError, KeyboardInterrupt):
                self.message = "操作已取消。"


def open_workbench(service: CrewlineService) -> None:
    try:
        curses.wrapper(Workbench(service).run)
    except KeyboardInterrupt:
        pass
