"""Workspace isolation layer (local Git worktrees, extensible to SSH/remote)."""

from crewline.workspace.git_worktree import (
    ExecutionWorkspace,
    LocalGitWorktreeManager,
    WorkspaceBackend,
)

__all__ = [
    "ExecutionWorkspace",
    "LocalGitWorktreeManager",
    "WorkspaceBackend",
]
