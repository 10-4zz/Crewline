"""Tests for the interactive startup Logo, NO_COLOR handling, narrow terminals, and script safety."""

from __future__ import annotations

import json
from pathlib import Path

from click.testing import CliRunner
import pytest

from crewline import __version__
from crewline.cli import main as cli_main
from crewline.cli.formatters import format_startup_banner, should_use_color


def test_startup_banner_full_width_with_color() -> None:
    banner, use_color = format_startup_banner(
        __version__,
        width=80,
        is_tty=True,
        environ={"TERM": "xterm-256color"},
    )
    assert use_color is True
    assert "\x1b[" in banner
    assert "▄██████▄" in banner
    assert "●" in banner and "─" in banner
    assert "Crewline" in banner
    assert __version__ in banner
    assert "多 Agent 任务管理" in banner
    assert "crewline --help" in banner


def test_startup_banner_respects_no_color() -> None:
    assert should_use_color(is_tty=True, environ={"NO_COLOR": "1", "TERM": "xterm-256color"}) is False
    banner, use_color = format_startup_banner(
        __version__,
        width=80,
        is_tty=True,
        environ={"NO_COLOR": "1", "TERM": "xterm-256color"},
    )
    assert use_color is False
    assert "\x1b[" not in banner
    assert "▄██████▄" in banner
    assert f"Crewline {__version__}" in banner


def test_startup_banner_compact_on_narrow_terminal() -> None:
    banner, _ = format_startup_banner(
        __version__,
        width=36,
        is_tty=True,
        environ={"NO_COLOR": "1"},
    )
    assert "▄██████▄" not in banner
    assert f"●─●─● Crewline {__version__}" in banner
    assert "多 Agent 任务管理" in banner
    assert "crewline --help" in banner


def test_cli_shows_logo_only_on_bare_interactive_invocation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = CliRunner()
    home = tmp_path / ".crewline"

    # 1. Bare interactive invocation -> shows Logo + version + short help.
    monkeypatch.setattr(cli_main, "_is_interactive_stdout", lambda: True)
    monkeypatch.setenv("NO_COLOR", "1")
    monkeypatch.setenv("COLUMNS", "80")
    res = runner.invoke(cli_main.cli, [])
    assert res.exit_code == 0, res.output
    assert "▄██████▄" in res.output
    assert f"Crewline {__version__}" in res.output
    assert "常用命令:" in res.output

    # 2. Bare non-interactive invocation -> standard Click help, no pixel Logo.
    monkeypatch.setattr(cli_main, "_is_interactive_stdout", lambda: False)
    res_pipe = runner.invoke(cli_main.cli, [])
    assert res_pipe.exit_code == 0, res_pipe.output
    assert "▄██████▄" not in res_pipe.output
    assert "Usage:" in res_pipe.output

    # 3. --version keeps exact format even when interactive.
    monkeypatch.setattr(cli_main, "_is_interactive_stdout", lambda: True)
    res_ver = runner.invoke(cli_main.cli, ["--version"])
    assert res_ver.exit_code == 0, res_ver.output
    assert res_ver.output.strip() == f"crewline, version {__version__}"
    assert "▄██████▄" not in res_ver.output

    # 4. --help stays standard help without Logo.
    res_help = runner.invoke(cli_main.cli, ["--help"])
    assert res_help.exit_code == 0, res_help.output
    assert "▄██████▄" not in res_help.output
    assert "Usage:" in res_help.output

    # 5. Subcommands and --json stay clean JSON without Logo.
    res_init = runner.invoke(cli_main.cli, ["--home", str(home), "init"])
    assert res_init.exit_code == 0, res_init.output
    assert "▄██████▄" not in res_init.output

    res_json = runner.invoke(cli_main.cli, ["--home", str(home), "doctor", "--json"])
    assert res_json.exit_code == 0, res_json.output
    assert "▄██████▄" not in res_json.output
    parsed = json.loads(res_json.output)
    assert isinstance(parsed, list) and len(parsed) > 0
