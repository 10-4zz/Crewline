"""Tests for parallel execution in isolated Git worktrees on the same repository."""

from pathlib import Path
import subprocess

import pytest

from crewline.domain.errors import WorkspaceError
from crewline.domain.models import TaskStatus
from crewline.engine.service import CrewlineService


def _init_git_repo(repo_dir: Path) -> None:
    repo_dir.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init"], cwd=str(repo_dir), check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.name", "Crewline Test"],
        cwd=str(repo_dir),
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.email", "test@crewline.local"],
        cwd=str(repo_dir),
        check=True,
        capture_output=True,
    )
    (repo_dir / ".gitignore").write_text(".crewline/\n", encoding="utf-8")
    (repo_dir / "README.md").write_text("# Demo Repo\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=str(repo_dir), check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "initial commit"],
        cwd=str(repo_dir),
        check=True,
        capture_output=True,
    )


def test_parallel_cards_same_repo_use_separate_worktrees(tmp_path: Path) -> None:
    repo_dir = tmp_path / "repo"
    _init_git_repo(repo_dir)

    service = CrewlineService(repo_dir / ".crewline")
    service.init_workspace()

    t1 = service.create_task(
        title="卡片A：修改共享文件",
        description="[mock:delay=0.25] [mock:file=shared.md] 卡片A内容",
        repo_path=repo_dir,
        assigned_agent="mock-dev",
    )
    t2 = service.create_task(
        title="卡片B：并行修改同名文件",
        description="[mock:delay=0.25] [mock:file=shared.md] 卡片B内容",
        repo_path=repo_dir,
        assigned_agent="mock-qa",
    )

    outcomes = service.run_tasks([t1.id, t2.id], max_workers=2)
    assert all(o.succeeded for o in outcomes)

    d1 = service.get_task_detail(t1.id).task
    d2 = service.get_task_detail(t2.id).task
    assert d1.status == TaskStatus.IN_REVIEW
    assert d2.status == TaskStatus.IN_REVIEW

    assert d1.worktree_path is not None and d2.worktree_path is not None
    assert d1.worktree_path != d2.worktree_path
    assert d1.worktree_branch == f"crewline/{t1.id}"
    assert d2.worktree_branch == f"crewline/{t2.id}"

    # Verify file isolation in each worktree AND main repo untouched.
    file_a = Path(d1.worktree_path) / "shared.md"
    file_b = Path(d2.worktree_path) / "shared.md"
    assert file_a.exists() and "卡片A" in file_a.read_text(encoding="utf-8")
    assert file_b.exists() and "卡片B" in file_b.read_text(encoding="utf-8")
    assert not (repo_dir / "shared.md").exists()

    # Diff view should show the new file.
    diff_a = service.get_diff(t1.id)
    assert "shared.md" in diff_a

    # Dirty worktree must be refused on cleanup_worktree=True, preserving files & in_review state.
    with pytest.raises(WorkspaceError):
        service.approve_task(t1.id, comment="LGTM", cleanup_worktree=True)
    assert service.get_task_detail(t1.id).task.status == TaskStatus.IN_REVIEW
    assert file_a.exists()

    # After committing deliverables so worktree is clean, cleanup removes directory and keeps branch.
    subprocess.run(["git", "add", "shared.md"], cwd=d1.worktree_path, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "deliver card A"],
        cwd=d1.worktree_path,
        check=True,
        capture_output=True,
    )
    approved, _ = service.approve_task(t1.id, comment="LGTM", cleanup_worktree=True)
    assert approved.status == TaskStatus.DONE
    assert approved.worktree_path is None
    assert approved.worktree_branch == f"crewline/{t1.id}"
    assert not Path(d1.worktree_path).exists()
    assert Path(d2.worktree_path).exists()
