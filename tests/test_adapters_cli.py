"""Simulated end-to-end tests for official CLI adapters (Codex, Claude Code, Grok Build)."""

from pathlib import Path
import textwrap

from crewline.domain.capabilities import AgentCapability
from crewline.domain.models import ProviderId, TaskStatus
from crewline.engine.service import CrewlineService


def _write_fake_cli(path: Path, body: str) -> Path:
    path.write_text("#!/usr/bin/env python3\n" + textwrap.dedent(body), encoding="utf-8")
    path.chmod(0o755)
    return path


def test_missing_cli_binaries_reported_by_doctor(tmp_path: Path) -> None:
    service = CrewlineService(tmp_path / ".crewline")
    service.init_workspace()
    service.add_agent(
        "codex-missing",
        ProviderId.CODEX,
        cli_bin="/nonexistent/codex-bin",
        overwrite=True,
    )
    service.add_agent(
        "claude-missing",
        ProviderId.CLAUDE_CODE,
        cli_bin="/nonexistent/claude-bin",
        overwrite=True,
    )
    service.add_agent(
        "grok-missing",
        ProviderId.GROK_BUILD,
        cli_bin="/nonexistent/grok-bin",
        overwrite=True,
    )

    for name in ("codex-missing", "claude-missing", "grok-missing"):
        report = service.doctor(agent_name=name)
        assert len(report) == 1
        item = report[0]
        assert item.ready is False
        assert item.status_label == "MISSING_CLI"
        assert item.missing_items
        assert item.setup_steps
        assert AgentCapability.EDIT_FILES in item.capabilities
        assert AgentCapability.RUN_TESTS in item.capabilities


def test_codex_adapter_simulated_cli_lifecycle(tmp_path: Path) -> None:
    fake_codex = _write_fake_cli(
        tmp_path / "fake-codex",
        """
        import pathlib, sys
        args = sys.argv[1:]
        if args[:2] == ["login", "status"]:
            print("Logged in using ChatGPT")
            sys.exit(0)
        if args and args[0] == "exec":
            prompt = sys.stdin.read()
            out_file = None
            if "-o" in args:
                out_file = pathlib.Path(args[args.index("-o") + 1])
            pathlib.Path("codex_result.txt").write_text("done:" + prompt[:40], encoding="utf-8")
            msg = "Codex 已完成工作区修改并验证通过"
            if out_file:
                out_file.write_text(msg, encoding="utf-8")
            print(msg)
            sys.exit(0)
        sys.exit(2)
        """,
    )

    service = CrewlineService(tmp_path / ".crewline")
    service.init_workspace()
    service.add_agent(
        "codex-sim",
        ProviderId.CODEX,
        model="gpt-5-codex",
        cli_bin=str(fake_codex),
        overwrite=True,
    )

    doctor_item = service.doctor("codex-sim")[0]
    assert doctor_item.ready is True
    assert doctor_item.status_label == "READY"

    work_dir = tmp_path / "workspace"
    work_dir.mkdir()
    task = service.create_task("Codex 模拟适配测试", "创建结果文件", work_dir, "codex-sim")
    outcome = service.run_tasks([task.id])[0]
    assert outcome.succeeded is True
    assert outcome.task is not None and outcome.task.status == TaskStatus.IN_REVIEW
    assert (work_dir / "codex_result.txt").exists()


def test_claude_code_and_grok_build_simulated_cli(tmp_path: Path) -> None:
    fake_claude = _write_fake_cli(
        tmp_path / "fake-claude",
        """
        import pathlib, sys
        args = sys.argv[1:]
        if args[:2] == ["auth", "status"]:
            print('{"loggedIn": true}')
            sys.exit(0)
        if "-p" in args:
            prompt = args[args.index("-p") + 1]
            assert "--model" in args
            pathlib.Path("claude_edit.txt").write_text(prompt[:50], encoding="utf-8")
            print("Claude Code 完成修改与测试")
            sys.exit(0)
        sys.exit(1)
        """,
    )
    fake_grok = _write_fake_cli(
        tmp_path / "fake-grok",
        """
        import pathlib, sys
        args = sys.argv[1:]
        if args[:2] == ["auth", "status"]:
            print("Authenticated as demo-user")
            sys.exit(0)
        if "-p" in args:
            assert "--model" in args
            pathlib.Path("grok_edit.txt").write_text("ok", encoding="utf-8")
            print("Grok Build 完成修改与测试")
            sys.exit(0)
        sys.exit(1)
        """,
    )
    fake_grok_logged_out = _write_fake_cli(
        tmp_path / "fake-grok-out",
        """
        import sys
        print("Not logged in")
        sys.exit(1)
        """,
    )

    service = CrewlineService(tmp_path / ".crewline")
    service.init_workspace()
    service.add_agent(
        "claude-sim",
        ProviderId.CLAUDE_CODE,
        model="claude-sonnet-4-5",
        cli_bin=str(fake_claude),
        overwrite=True,
    )
    service.add_agent(
        "grok-sim",
        ProviderId.GROK_BUILD,
        model="grok-code-fast-1",
        cli_bin=str(fake_grok),
        overwrite=True,
    )
    service.add_agent(
        "grok-logged-out",
        ProviderId.GROK_BUILD,
        cli_bin=str(fake_grok_logged_out),
        overwrite=True,
    )

    assert service.doctor("claude-sim")[0].ready is True
    assert service.doctor("grok-sim")[0].ready is True
    out_doc = service.doctor("grok-logged-out")[0]
    assert out_doc.ready is False
    assert out_doc.status_label == "NOT_LOGGED_IN"

    work_dir = tmp_path / "proj"
    work_dir.mkdir()
    t_claude = service.create_task("Claude 模拟任务", "修改文件", work_dir, "claude-sim")
    t_grok = service.create_task("Grok 模拟任务", "修改文件", work_dir, "grok-sim")

    outcomes = service.run_tasks([t_claude.id, t_grok.id], max_workers=2)
    assert all(o.succeeded for o in outcomes)
    assert (work_dir / "claude_edit.txt").exists()
    assert (work_dir / "grok_edit.txt").exists()
