"""Git worktree manager for parallel isolated task execution."""

from __future__ import annotations

import fcntl
import os
import stat
import subprocess
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from crewline.domain.errors import WorkspaceError
from crewline.domain.models import TaskCard, WorkspaceChanges
from crewline.runtime import clean_subprocess_env

MAX_UNTRACKED_FILE_BYTES = 4096
MAX_UNTRACKED_TOTAL_BYTES = 64 * 1024
MAX_UNTRACKED_FILES_PREVIEW = 50


@dataclass(slots=True)
class ExecutionWorkspace:
    """Resolved execution directory for a task run."""

    cwd: Path
    repo_root: Path | None
    is_git_worktree: bool
    branch: str | None = None
    base_commit: str | None = None


class WorkspaceBackend(Protocol):
    """Protocol for execution workspaces (local Git worktree now, SSH/remote later)."""

    def prepare(self, task: TaskCard) -> ExecutionWorkspace: ...

    def summarize_changes(self, workspace: ExecutionWorkspace) -> WorkspaceChanges: ...

    def get_diff(
        self,
        worktree_path: Path,
        *,
        base_commit: str | None = None,
        branch: str | None = None,
    ) -> str: ...

    def cleanup(self, task: TaskCard) -> None: ...


class LocalGitWorktreeManager:
    """Creates, inspects, and safely cleans up one isolated Git worktree per task card."""

    _thread_lock = threading.Lock()

    def __init__(self, state_dir: Path) -> None:
        self.state_dir = state_dir
        self.worktrees_dir = state_dir / "worktrees"
        self.lock_file = state_dir / ".git-worktree.lock"

    def _run_git(self, args: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", *args],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=clean_subprocess_env(),
            check=False,
        )

    def _run_git_bytes(self, args: list[str], cwd: Path) -> subprocess.CompletedProcess[bytes]:
        return subprocess.run(
            ["git", *args],
            cwd=str(cwd),
            capture_output=True,
            text=False,
            env=clean_subprocess_env(),
            check=False,
        )

    def _run_git_checked(
        self,
        args: list[str],
        cwd: Path,
        action: str,
    ) -> subprocess.CompletedProcess[str]:
        proc = self._run_git(args, cwd=cwd)
        if proc.returncode != 0:
            detail = (proc.stderr or proc.stdout).strip() or f"exit={proc.returncode}"
            raise WorkspaceError(f"{action}失败：{detail}")
        return proc

    def _run_git_bytes_checked(
        self,
        args: list[str],
        cwd: Path,
        action: str,
    ) -> subprocess.CompletedProcess[bytes]:
        proc = self._run_git_bytes(args, cwd=cwd)
        if proc.returncode != 0:
            raw_err = proc.stderr or proc.stdout
            detail = raw_err.decode("utf-8", errors="replace").strip() or f"exit={proc.returncode}"
            raise WorkspaceError(f"{action}失败：{detail}")
        return proc

    @staticmethod
    def _parse_nul_paths(raw: bytes) -> list[str]:
        return [os.fsdecode(part) for part in raw.split(b"\x00") if part]

    def _detect_git_root(self, path: Path) -> Path | None:
        if not path.exists():
            raise WorkspaceError(f"工作区路径不存在：{path}")
        proc = self._run_git(["rev-parse", "--show-toplevel"], cwd=path)
        if proc.returncode != 0:
            return None
        return Path(proc.stdout.strip()).resolve()

    def _resolve_commit(self, cwd: Path, ref: str) -> str | None:
        proc = self._run_git(["rev-parse", "--verify", f"{ref}^{{commit}}"], cwd=cwd)
        if proc.returncode != 0:
            return None
        sha = proc.stdout.strip()
        return sha or None

    def _is_valid_commit(self, cwd: Path, sha: str) -> bool:
        proc = self._run_git(["cat-file", "-t", sha], cwd=cwd)
        return proc.returncode == 0 and proc.stdout.strip() == "commit"

    def _has_commits(self, repo_root: Path) -> bool:
        return self._resolve_commit(repo_root, "HEAD") is not None

    def _branch_exists(self, repo_root: Path, branch: str) -> bool:
        proc = self._run_git(
            ["show-ref", "--verify", "--quiet", f"refs/heads/{branch}"],
            cwd=repo_root,
        )
        return proc.returncode == 0

    def _is_valid_worktree(self, worktree_path: Path) -> bool:
        if not worktree_path.exists() or not worktree_path.is_dir():
            return False
        resolved = worktree_path.resolve()
        proc = self._run_git(["rev-parse", "--show-toplevel"], cwd=resolved)
        if proc.returncode != 0:
            return False
        return Path(proc.stdout.strip()).resolve() == resolved

    def _require_valid_worktree(self, worktree_path: Path, action: str) -> Path:
        if not worktree_path.exists() or not worktree_path.is_dir():
            raise WorkspaceError(f"Worktree 路径不存在：{worktree_path}")
        resolved = worktree_path.resolve()
        top_proc = self._run_git_checked(
            ["rev-parse", "--show-toplevel"],
            cwd=resolved,
            action=action,
        )
        top_path = Path(top_proc.stdout.strip()).resolve()
        if top_path != resolved:
            raise WorkspaceError(
                f"路径不是独立的有效 Git worktree：{resolved}（Git 解析根目录为 {top_path}）。"
            )
        return resolved

    def prepare(self, task: TaskCard) -> ExecutionWorkspace:
        """Prepare an isolated Git worktree for task, or fall back to plain directory."""
        repo_path = Path(task.repo_path).resolve()
        repo_root = self._detect_git_root(repo_path)
        if repo_root is None:
            return ExecutionWorkspace(
                cwd=repo_path,
                repo_root=None,
                is_git_worktree=False,
                branch=None,
                base_commit=None,
            )

        if not self._has_commits(repo_root):
            raise WorkspaceError(
                f"Git 仓库 {repo_root} 尚无提交记录，无法创建独立 worktree。"
                "请先执行一次初始 `git commit`。"
            )

        self.worktrees_dir.mkdir(parents=True, exist_ok=True)
        worktree_path = (self.worktrees_dir / task.id).resolve()
        branch = task.worktree_branch or f"crewline/{task.id}"
        base_commit = task.worktree_base_commit

        # Serialize only the quick git worktree metadata operation to avoid .git lock races
        # while multiple cards run in parallel.
        self.lock_file.parent.mkdir(parents=True, exist_ok=True)
        with self._thread_lock:
            with self.lock_file.open("w", encoding="utf-8") as lock_fh:
                fcntl.flock(lock_fh.fileno(), fcntl.LOCK_EX)
                try:
                    if self._is_valid_worktree(worktree_path):
                        return ExecutionWorkspace(
                            cwd=worktree_path,
                            repo_root=repo_root,
                            is_git_worktree=True,
                            branch=branch,
                            base_commit=base_commit,
                        )

                    self._run_git(["worktree", "prune"], cwd=repo_root)
                    if self._branch_exists(repo_root, branch):
                        proc = self._run_git(
                            ["worktree", "add", str(worktree_path), branch],
                            cwd=repo_root,
                        )
                    else:
                        head_sha = self._resolve_commit(repo_root, "HEAD")
                        if head_sha is None:
                            raise WorkspaceError(
                                f"无法解析 Git 仓库 {repo_root} 的 HEAD 提交。"
                            )
                        proc = self._run_git(
                            ["worktree", "add", "-b", branch, str(worktree_path), head_sha],
                            cwd=repo_root,
                        )
                        if base_commit is None:
                            base_commit = head_sha
                    if proc.returncode != 0:
                        detail = (proc.stderr or proc.stdout).strip()
                        raise WorkspaceError(
                            f"为任务卡 {task.id} 创建 Git worktree 失败：{detail}"
                        )
                finally:
                    fcntl.flock(lock_fh.fileno(), fcntl.LOCK_UN)

        return ExecutionWorkspace(
            cwd=worktree_path,
            repo_root=repo_root,
            is_git_worktree=True,
            branch=branch,
            base_commit=base_commit,
        )

    def summarize_changes(self, workspace: ExecutionWorkspace) -> WorkspaceChanges:
        if not workspace.is_git_worktree or not workspace.cwd.exists():
            return WorkspaceChanges(is_git_worktree=False)

        seen: set[str] = set()
        changed_files: list[str] = []

        def collect(args: list[str]) -> None:
            proc = self._run_git_bytes(args, cwd=workspace.cwd)
            if proc.returncode != 0:
                return
            for rel in self._parse_nul_paths(proc.stdout):
                if rel not in seen:
                    seen.add(rel)
                    changed_files.append(rel)

        has_base = bool(
            workspace.base_commit and self._is_valid_commit(workspace.cwd, workspace.base_commit)
        )
        if has_base and workspace.base_commit:
            collect(["diff", "--name-only", "-z", f"{workspace.base_commit}..HEAD"])
        collect(["diff", "--cached", "--name-only", "-z"])
        collect(["diff", "--name-only", "-z"])
        collect(["ls-files", "--others", "--exclude-standard", "-z"])

        stat_ref = workspace.base_commit if has_base and workspace.base_commit else "HEAD"
        diff_proc = self._run_git(
            ["-c", "core.quotepath=false", "diff", "--stat", stat_ref],
            cwd=workspace.cwd,
        )
        diff_stat = diff_proc.stdout.strip() if diff_proc.returncode == 0 else ""
        return WorkspaceChanges(
            is_git_worktree=True,
            changed_files=changed_files,
            diff_stat=diff_stat,
        )

    def _format_untracked_file(
        self,
        worktree_root: Path,
        rel: str,
        remaining_budget: int,
    ) -> tuple[str, int]:
        """Safely render a single untracked file with symlink and size protections."""
        rel_path = Path(rel)
        if not rel_path.parts or rel_path.is_absolute() or ".." in rel_path.parts:
            return f"--- {rel} ---\n[已跳过：非法工作区路径]", 0

        current = worktree_root
        for part in rel_path.parts[:-1]:
            current = current / part
            if current.is_symlink():
                try:
                    target = os.readlink(current)
                except OSError:
                    target = "<无法读取目标>"
                return (
                    f"--- {rel} ---\n[已跳过符号链接目录：{part} -> {target}（不跟随读取目标内容）]",
                    0,
                )

        file_path = current / rel_path.parts[-1]
        if file_path.is_symlink():
            try:
                target = os.readlink(file_path)
            except OSError:
                target = "<无法读取目标>"
            return f"--- {rel} ---\n[符号链接 -> {target}（已跳过，不跟随读取目标内容）]", 0

        if remaining_budget <= 0:
            return (
                f"--- {rel} ---\n[已省略：达到未跟踪文件总预览大小上限（{MAX_UNTRACKED_TOTAL_BYTES} 字节）]",
                0,
            )

        open_flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        try:
            fd = os.open(str(file_path), open_flags)
        except OSError as exc:
            return f"--- {rel} ---\n<无法安全读取: {exc}>", 0

        try:
            st = os.fstat(fd)
            if not stat.S_ISREG(st.st_mode):
                return f"--- {rel} ---\n[非普通文件，已跳过]", 0

            per_file_cap = min(MAX_UNTRACKED_FILE_BYTES, remaining_budget)
            with os.fdopen(fd, "rb") as fh:
                fd = -1
                raw = fh.read(per_file_cap + 1)
        except OSError as exc:
            return f"--- {rel} ---\n<无法读取: {exc}>", 0
        finally:
            if fd >= 0:
                try:
                    os.close(fd)
                except OSError:
                    pass

        if b"\x00" in raw:
            return f"--- {rel} ---\n[二进制文件，已省略内容，大小 {st.st_size} 字节]", 0

        truncated = len(raw) > per_file_cap or st.st_size > per_file_cap
        display_bytes = raw[:per_file_cap]
        content = display_bytes.decode("utf-8", errors="replace").rstrip("\n")
        if truncated:
            note = f"... [已截断：文件共 {st.st_size} 字节，当前仅展示前 {per_file_cap} 字节]"
            content = f"{content}\n{note}" if content else note
        return f"--- {rel} ---\n{content}", len(display_bytes)

    def get_diff(
        self,
        worktree_path: Path,
        *,
        base_commit: str | None = None,
        branch: str | None = None,
    ) -> str:
        worktree_root = self._require_valid_worktree(worktree_path, action="校验 Git worktree")

        head_sha = self._run_git_checked(
            ["rev-parse", "--verify", "HEAD^{commit}"],
            cwd=worktree_root,
            action="读取当前 worktree HEAD 提交",
        ).stdout.strip()
        branch_proc = self._run_git_checked(
            ["rev-parse", "--abbrev-ref", "HEAD"],
            cwd=worktree_root,
            action="读取当前 worktree 分支",
        )
        display_branch = branch or branch_proc.stdout.strip()

        has_reliable_base = False
        base_warning: str | None = None
        if base_commit:
            if self._is_valid_commit(worktree_root, base_commit):
                has_reliable_base = True
            else:
                base_warning = (
                    f"[提示] 任务卡记录的基准提交 {base_commit} 在当前仓库中不可达，"
                    "无法准确恢复已提交差异；以下展示当前工作区的已暂存、未暂存与新增未跟踪变更。"
                )
        else:
            base_warning = (
                "[提示] 该任务卡缺少可靠的基准提交记录（可能为旧版本数据），"
                "无法准确计算已提交差异；以下展示当前工作区的已暂存、未暂存与新增未跟踪变更。"
            )

        status_out = self._run_git_checked(
            ["-c", "core.quotepath=false", "status", "--short", "-uall"],
            cwd=worktree_root,
            action="读取 Git 工作区状态",
        ).stdout.rstrip("\n")

        commit_log_out = ""
        committed_diff_out = ""
        if has_reliable_base and base_commit:
            commit_log_out = self._run_git_checked(
                [
                    "-c",
                    "core.quotepath=false",
                    "log",
                    "--oneline",
                    "--no-decorate",
                    f"{base_commit}..HEAD",
                ],
                cwd=worktree_root,
                action="读取基准提交后的提交记录",
            ).stdout.strip()
            committed_diff_out = self._run_git_checked(
                ["-c", "core.quotepath=false", "diff", f"{base_commit}..HEAD"],
                cwd=worktree_root,
                action="读取基准提交后的已提交差异",
            ).stdout.strip()

        staged_diff_out = self._run_git_checked(
            ["-c", "core.quotepath=false", "diff", "--cached"],
            cwd=worktree_root,
            action="读取已暂存变更",
        ).stdout.strip()
        unstaged_diff_out = self._run_git_checked(
            ["-c", "core.quotepath=false", "diff"],
            cwd=worktree_root,
            action="读取未暂存变更",
        ).stdout.strip()
        untracked_raw = self._run_git_bytes_checked(
            ["ls-files", "--others", "--exclude-standard", "-z"],
            cwd=worktree_root,
            action="列举新增未跟踪文件",
        ).stdout
        untracked = self._parse_nul_paths(untracked_raw)

        parts: list[str] = [
            f"分支: {display_branch} (HEAD={head_sha[:12]})",
            (
                f"基准提交: {base_commit[:12]} ({base_commit})"
                if has_reliable_base and base_commit
                else "基准提交: 未知（无可靠基准记录）"
            ),
        ]
        if base_warning:
            parts.append(base_warning)

        has_any_change = False

        if status_out:
            has_any_change = True
            parts.extend(["", "=== git status --short ===", status_out])

        if has_reliable_base and base_commit and (commit_log_out or committed_diff_out):
            has_any_change = True
            parts.extend(["", f"=== 已提交变更 ({base_commit[:12]}..HEAD) ==="])
            if commit_log_out:
                parts.extend(["--- 提交记录 ---", commit_log_out])
            if committed_diff_out:
                if commit_log_out:
                    parts.append("")
                parts.append(committed_diff_out)

        if staged_diff_out:
            has_any_change = True
            parts.extend(["", "=== 已暂存变更 (git diff --cached) ===", staged_diff_out])

        if unstaged_diff_out:
            has_any_change = True
            parts.extend(["", "=== 未暂存变更 (git diff) ===", unstaged_diff_out])

        if untracked:
            has_any_change = True
            parts.extend(["", "=== 新增未跟踪文件 ==="])
            remaining_budget = MAX_UNTRACKED_TOTAL_BYTES
            for idx, rel in enumerate(untracked):
                if idx >= MAX_UNTRACKED_FILES_PREVIEW:
                    omitted = len(untracked) - MAX_UNTRACKED_FILES_PREVIEW
                    parts.append(
                        f"... [其余 {omitted} 个未跟踪文件已省略预览，完整列表见 git status]"
                    )
                    break
                block, used = self._format_untracked_file(
                    worktree_root, rel, remaining_budget
                )
                remaining_budget = max(0, remaining_budget - used)
                parts.append(block)

        if not has_any_change:
            parts.append("")
            if has_reliable_base:
                parts.append("（从基准提交到当前工作区无文件变更）")
            else:
                parts.append("（当前工作区无已暂存、未暂存或新增未跟踪变更；因缺少基准提交，未检查历史提交差异）")

        return "\n".join(parts)

    def cleanup(self, task: TaskCard) -> None:
        """Safely remove a clean Git worktree using non-force removal while preserving the branch."""
        if not task.worktree_path:
            return

        worktree_path = Path(task.worktree_path)
        repo_path = Path(task.repo_path)
        repo_root = self._detect_git_root(repo_path)
        if repo_root is None:
            raise WorkspaceError(
                f"无法清理 worktree：目标仓库路径不是有效 Git 仓库（{repo_path}）。"
            )
        if not worktree_path.exists():
            raise WorkspaceError(
                f"无法清理 worktree：目录不存在（{worktree_path}）。"
            )

        self.lock_file.parent.mkdir(parents=True, exist_ok=True)
        with self._thread_lock:
            with self.lock_file.open("w", encoding="utf-8") as lock_fh:
                fcntl.flock(lock_fh.fileno(), fcntl.LOCK_EX)
                try:
                    worktree_root = self._require_valid_worktree(
                        worktree_path, action="校验待清理 worktree"
                    )

                    status_proc = self._run_git_checked(
                        [
                            "-c",
                            "core.quotepath=false",
                            "status",
                            "--porcelain=v1",
                            "-uall",
                            "--ignored",
                        ],
                        cwd=worktree_root,
                        action="检查 worktree 干净状态",
                    )
                    dirty_lines = [
                        line for line in status_proc.stdout.splitlines() if line.strip()
                    ]
                    if dirty_lines:
                        preview = "；".join(dirty_lines[:5])
                        if len(dirty_lines) > 5:
                            preview += f" 等共 {len(dirty_lines)} 项"
                        raise WorkspaceError(
                            f"拒绝清理脏 worktree（存在未提交、未跟踪或被忽略的本地文件：{preview}）。"
                            "已保留全部文件、分支、卡片关联与原待审核状态；"
                            f"请先提交或处理改动后再清理，或执行普通 `crewline review approve {task.id}` 保留 worktree。"
                        )

                    remove_proc = self._run_git(
                        ["worktree", "remove", str(worktree_path)],
                        cwd=repo_root,
                    )
                    if remove_proc.returncode != 0:
                        detail = (
                            (remove_proc.stderr or remove_proc.stdout).strip()
                            or f"exit={remove_proc.returncode}"
                        )
                        raise WorkspaceError(
                            f"Git 清理 worktree 失败：{detail}。"
                            "已保留卡片关联与原待审核状态。"
                        )
                    if worktree_path.exists():
                        raise WorkspaceError(
                            f"Git worktree 清理后目录仍存在：{worktree_path}。"
                        )
                finally:
                    fcntl.flock(lock_fh.fileno(), fcntl.LOCK_UN)
