# Crewline

[English](README.md) · [简体中文](README.zh-CN.md)

Codex fixes a parser bug while Claude Code writes regression tests. You review both diffs on one board.

Crewline gives each task its own Git worktree and terminal session, so multiple AI coding agents can work in parallel on the same repository while you track progress and review every change in one place.

```text
  ●─●─● Crewline 0.1.0                          (illustrative board preview)
  ~/your-project
  待办 0   处理中 1   待审核 1   审核完成 0

  task-001 待审核  codex   Fix parser edge case
  task-002 处理中  claude  Add regression tests
```

## Install

Requires **Python 3.11+**, **Git**, and **tmux**. Install Crewline in its own virtual environment so your system Python and `pip` stay untouched:

```bash
python3 -m venv ~/.crewline-venv
source "$HOME/.crewline-venv/bin/activate"
pip install git+https://github.com/10-4zz/Crewline.git
crewline --version
```

In a new terminal, activate the environment again with `source "$HOME/.crewline-venv/bin/activate"`, or run `~/.crewline-venv/bin/crewline` directly.

## First Run (No Account Needed)

Built-in mock agents (`mock-dev` and `mock-qa`) let you try the parallel board before connecting any model accounts.

1. Open an existing Git repository (with at least one commit) and launch Crewline:
   ```bash
   cd /path/to/your-git-repo
   crewline
   ```
   > Note: The v0.1 board UI and prompts are currently in Chinese (`待办` = todo, `处理中` = in progress, `待审核` = in review, `审核完成` = done), while all shortcuts are single ASCII keys.
2. Press `i` to initialize `.crewline/` (add `.crewline/` to `.gitignore`).
3. Create and start your first card: press `n`, enter a title and description, type `mock-dev` and press **Enter**, then press `s` to start it.
4. Create and start a second card in parallel: press `n`, enter a title and description, type `mock-qa` and press **Enter**, then press `s`.
   - Each card runs in its own worktree (`.crewline/worktrees/<task_id>`, branch `crewline/<task_id>`). The default mock agent writes a sample file at `crewline_outputs/<task_id>.md` and moves the card to **in review** (`待审核`).
5. Review each card:
   - Press `d` to inspect the full diff (committed, staged, unstaged, and untracked changes) and `l` for logs.
   - Press `r` to reject with a comment back to **todo** (`待办`), then `s` to retry with your feedback.
   - Press `p`, then type `y` and press **Enter** to mark the card **done** (`审核完成`).

Press `q` to close the board while running sessions stay alive in `tmux`; run `crewline` again in the same directory (locally or over SSH) to reconnect. You can also press `x` to move an idle card to the recycle bin, `b` to view the recycle bin, and `u` to restore it.

**After approval**: Approving marks the card complete and keeps its worktree and branch intact—it does not auto-commit or auto-merge. Inspect and commit the files you want inside `.crewline/worktrees/<task_id>`, then merge or open a PR with your normal Git workflow.

## Connect Real Agents

Crewline pre-configures named agents when you initialize a project. For example, sign in to Codex once, verify it, and type `codex` when assigning a card on the board:

```bash
codex login
crewline doctor --agent codex
```

| Built-in Agent | Setup | What It Does |
|---|---|---|
| `codex` | `codex login` | Interactive CLI in `tmux`; analyzes, edits files, runs tests |
| `claude` | `claude auth login` | Interactive CLI in `tmux`; analyzes, edits files, runs tests |
| `grok` | `grok login` | Interactive CLI in `tmux`; analyzes, edits files, runs tests |
| `gemini` | `export GEMINI_API_KEY=...` | Background API call; analysis output only (v0.1) |
| `deepseek` | `export DEEPSEEK_API_KEY=...` | Background API call; analysis output only (v0.1) |
| `mock-dev`, `mock-qa` | None | Local mock; writes `crewline_outputs/<task_id>.md` |

- Interactive CLI sessions (`Enter` to attach, `Ctrl+b` then `d` to detach) prompt you to enter `y` on exit before moving the card to review. `grok` prints the task prompt into the pane scrollback so you can copy it into its input box.
- API-key and mock runs move to review automatically on success. `.crewline/config.toml` stores only environment variable names, never raw keys.

## CLI Reference

Most workflows are also available from the command line:

```bash
crewline --help
crewline doctor
crewline task list
```

## License

Crewline is released under the [MIT License](LICENSE).
