"""End-to-end CLI workflow tests covering create, assign, parallel run, logs, reject, retry, approve, and failure."""

import json
from pathlib import Path
import subprocess

from click.testing import CliRunner

from crewline.cli.main import cli


def _init_git_repo(repo_dir: Path) -> None:
    repo_dir.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init"], cwd=str(repo_dir), check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Demo"], cwd=str(repo_dir), check=True)
    subprocess.run(
        ["git", "config", "user.email", "demo@crewline.local"],
        cwd=str(repo_dir),
        check=True,
    )
    (repo_dir / ".gitignore").write_text(".crewline/\n", encoding="utf-8")
    (repo_dir / "app.py").write_text("print('hello')\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=str(repo_dir), check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=str(repo_dir), check=True)


def test_full_cli_demo_workflow(tmp_path: Path) -> None:
    repo = tmp_path / "demo-repo"
    _init_git_repo(repo)
    home = repo / ".crewline"
    runner = CliRunner()

    def invoke(*args: str):
        return runner.invoke(cli, ["--home", str(home), *args])

    res = invoke("init")
    assert res.exit_code == 0, res.output

    res = invoke("doctor", "--json")
    assert res.exit_code == 0, res.output
    doctor_data = json.loads(res.output)
    by_name = {item["agent_name"]: item for item in doctor_data}
    assert by_name["mock-dev"]["ready"] is True
    assert "修改工作区文件" in by_name["mock-dev"]["capabilities_zh"]

    # Create two cards and assign them.
    res = invoke(
        "task",
        "create",
        "--title",
        "实现健康检查接口",
        "--description",
        "[mock:file=docs/health.md] 新增健康检查文档",
        "--repo",
        str(repo),
    )
    assert res.exit_code == 0, res.output
    assert "task-001" in res.output

    res = invoke(
        "task",
        "create",
        "--title",
        "补充单元测试说明",
        "--description",
        "[mock:file=docs/tests.md] 新增测试说明",
        "--repo",
        str(repo),
        "--agent",
        "mock-qa",
    )
    assert res.exit_code == 0, res.output
    assert "task-002" in res.output

    res = invoke("task", "assign", "task-001", "mock-dev")
    assert res.exit_code == 0, res.output

    # Run both cards in parallel.
    res = invoke("run", "task-001", "task-002")
    assert res.exit_code == 0, res.output
    assert "待审核(in_review)" in res.output

    # View logs and diff.
    res = invoke("logs", "task-001")
    assert res.exit_code == 0, res.output
    assert "docs/health.md" in res.output

    res = invoke("diff", "task-001")
    assert res.exit_code == 0, res.output
    assert "docs/health.md" in res.output

    # Reject task-001 with comment -> back to todo -> retry -> approve both.
    res = invoke("review", "reject", "task-001", "--comment", "请补充错误码与边界说明")
    assert res.exit_code == 0, res.output
    assert "待办(todo)" in res.output

    res = invoke("run", "task-001")
    assert res.exit_code == 0, res.output
    assert "已响应人工审核退回意见" in res.output

    res = invoke("logs", "task-001")
    assert res.exit_code == 0, res.output
    assert "请补充错误码与边界说明" in res.output

    res = invoke("review", "approve", "task-001", "--comment", "修改清晰，通过")
    assert res.exit_code == 0, res.output
    assert "审核完成(done)" in res.output

    res = invoke("review", "approve", "task-002", "--comment", "通过")
    assert res.exit_code == 0, res.output

    res = invoke("task", "list", "--status", "done", "--json")
    assert res.exit_code == 0, res.output
    done_tasks = json.loads(res.output)
    assert {t["id"] for t in done_tasks} == {"task-001", "task-002"}

    # Test failure -> returns to todo with error & log preserved -> retry succeeds.
    res = invoke(
        "task",
        "create",
        "--title",
        "失败回退测试",
        "--description",
        "[mock:fail-once] 首次失败后重试成功",
        "--repo",
        str(repo),
        "--agent",
        "mock-dev",
    )
    assert res.exit_code == 0, res.output

    res = invoke("run", "task-003")
    assert res.exit_code == 1
    assert "待办(todo)" in res.output

    res = invoke("task", "show", "task-003", "--json")
    assert res.exit_code == 0
    detail = json.loads(res.output)
    assert detail["task"]["status"] == "todo"
    assert detail["task"]["last_error"]
    assert len(detail["runs"]) == 1
    assert detail["runs"][0]["status"] == "failed"

    res = invoke("run", "task-003")
    assert res.exit_code == 0, res.output
    assert "待审核(in_review)" in res.output
