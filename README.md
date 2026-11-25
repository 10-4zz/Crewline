# Crewline

[English](README.md) · [简体中文](README.zh-CN.md)

Crewline is a local workspace for coordinating multiple AI coding agents on the same repository, currently provided through a terminal workbench and CLI (`v0.1.0`).

Task cards follow a four-state flow: `todo` → `in_progress` → `in_review` → `done`. Both failed runs and rejected reviews return to `todo`, preserving failure reasons, logs, or review comments for the next retry.

- Each card runs in its own isolated Git worktree (`.crewline/worktrees/<task_id>`, branch `crewline/<task_id>`) and records its original base commit. SQLite locking prevents starting the same card twice.
- `crewline diff` and workbench `d` show committed changes since the base commit alongside staged, unstaged, and untracked files, with size limits and without following symlinks. `review approve --cleanup-worktree` checks for uncommitted, untracked, and `.gitignore`-ignored files before running non-force `git worktree remove`. When the worktree is dirty, cleanup is refused and the card stays in `in_review` with its files and branch intact.
- Reviewers can approve work to `done`, reject with feedback passed into the next retry, or move idle cards into a recoverable recycle bin while keeping logs, history, and Git worktrees.
- You can combine official CLI agents (`codex`, `claude_code`, `grok_build`), API-key analysts (`gemini`, `deepseek`), and zero-account `mock` agents. Config stores only environment variable names (such as `GEMINI_API_KEY`), never raw keys.

## Quickstart

### Install from Source

Requires **Python 3.11+**, **Git**, and **tmux** (for interactive and detached workbench sessions). Clone Crewline and install it with Python 3.11 or newer:

```bash
git clone https://github.com/10-4zz/Crewline.git
cd Crewline
python3 -m pip install -e .
crewline --version
```

> Offline environments where `setuptools` and `click` are already installed can run `python3 -m pip install --no-build-isolation --no-deps -e .`.

### Quick Demo (No Model Account Required)

The demo runs inside a subshell with `set -e` and creates a fresh temporary Git repository before writing any files. It walks through init, diagnostics, two parallel mock cards in separate worktrees, logs and diff inspection, reject-and-retry, and approval:

```bash
(
  set -e
  DEMO_DIR="$(mktemp -d)"
  cd "$DEMO_DIR"

  git init
  git config user.name "Crewline Demo"
  git config user.email "demo@crewline.local"
  printf ".crewline/\n" > .gitignore
  printf "# Crewline Demo\n" > README.md
  git add .
  git commit -m "chore: initial commit"

  # 1. Initialize local state and check configured agents
  crewline init
  crewline doctor

  # 2. Create two cards and assign them to built-in mock agents
  crewline task create \
    --title "Draft auth module design" \
    --description "[mock:delay=0.3] [mock:file=docs/auth.md] Write authentication design notes"
  crewline task assign task-001 mock-dev

  crewline task create \
    --title "Add billing test checklist" \
    --description "[mock:delay=0.3] [mock:file=docs/billing-tests.md] Write billing regression checklist" \
    --agent mock-qa

  # 3. Run both cards in parallel (each in its own Git worktree)
  crewline run task-001 task-002

  # 4. Inspect logs and full worktree diff
  crewline logs task-001
  crewline diff task-001

  # 5. Reject task-001 with feedback, retry, and inspect updated diff
  crewline review reject task-001 --comment "Add token expiration and error code notes"
  crewline run task-001
  crewline diff task-001

  # 6. Approve both cards to done
  crewline review approve task-001 --comment "Looks good"
  crewline review approve task-002 --comment "Checklist complete"
  crewline task list
  crewline task show task-001
  echo "Demo workspace: $DEMO_DIR"
)
```

### Install from a Local Linux x86_64 Package

This repository currently distributes **source code** (no prebuilt public GitHub Release archives are published yet). If you already have a locally built `crewline-<version>-linux-x86_64.tar.gz` and its `.sha256` file—produced into `dist/` by `./scripts/build_linux_dist.sh`—install it without Python, `pip`, or `sudo`:

```bash
# Default install to ~/.local/bin/crewline (runtime under ~/.local/share/crewline/releases/)
./install.sh dist/crewline-0.1.0-linux-x86_64.tar.gz
export PATH="$HOME/.local/bin:$PATH"
crewline --version

# Or install into a custom / temporary prefix (absolute or relative paths supported)
./install.sh dist/crewline-0.1.0-linux-x86_64.tar.gz --prefix /tmp/crewline-test
# Optional flags: --bin-dir <dir> --data-dir <dir> --checksum <file.sha256>
```

- Target hosts need Linux `x86_64`, system `git` and `tmux`, and `glibc` at least `2.31` as well as at least the archive's `build_glibc`. Current testing covers Ubuntu `glibc 2.31`, and archives built on newer hosts require that newer `glibc`.
- Before modifying anything, `install.sh` checks architecture, `glibc`, `git`, `tmux`, and SHA-256. It then smoke-tests the staged binary with `--version` and updates `bin/crewline` with a single atomic symlink replace. On any error, it cleans up staging files and leaves any existing entrypoint untouched.

## Terminal Workbench

Run `crewline` inside a project directory to open the interactive terminal board (requires `tmux` to launch sessions).

| Key | Action |
|---|---|
| `i` | Initialize `.crewline/` workspace (when uninitialized) |
| `↑` / `↓` or `k` / `j` | Move selection |
| `n` | Create a card (title, description, optional agent picker or Enter to skip) |
| `a` | Assign or reassign a configured agent |
| `s` | Start the selected `todo` card in a detached tmux session |
| `Enter` | Attach to the selected card's active session |
| `Ctrl+b` then `d` | Detach from the task terminal and return to the workbench |
| `l` / `d` | View logs or full Git diff (read-only, also available in the recycle bin) |
| `p` / `r` | Approve (`in_review` → `done`, keeps worktree) or reject with comment (`in_review` → `todo`) |
| `x` | Move a non-running card to the recycle bin (prompts with card ID confirmation) |
| `b` | Toggle between active cards and the recycle bin |
| `u` | Restore the selected card from the recycle bin (keeps ID, status, assignment, history, worktree) |
| `q` | Quit the workbench while active background sessions keep running |

- Pressing `q` closes only the board UI, leaving running tasks alive in detached tmux sessions. Re-run `crewline` in the same project directory (locally or over standard SSH to the same host) to reconnect. If a background session terminates unexpectedly, reopening the workbench marks that run interrupted and keeps the card and worktree for retry.
- Start, assign, review, and create actions are blocked while browsing the recycle bin.

## Providers & Credentials

| Provider (`provider`) | Auth Method | Capabilities | Current Verification Status |
|---|---|---|---|
| `mock` | None (`none`) | Analysis, edit files, run tests | **Verified** across CLI workflow, automated tests, and Linux x86_64 frozen package E2E |
| `codex` | Official CLI (`official_cli`) | Analysis, edit files, run tests | **Host/source live E2E verified** (`codex-cli 0.160.0`, ChatGPT login, `gpt-6.1-terra`). Not yet live-tested inside the frozen package. |
| `claude_code` | Official CLI (`official_cli`) | Analysis, edit files, run tests | **Mock CLI tested only** (`claude` not installed on host) |
| `grok_build` | Official CLI (`official_cli`) | Analysis, edit files, run tests | **Mock CLI tested only** (`grok` not installed on host) |
| `gemini` | API Key (`api_key`, env ref) | Analysis output | **Local HTTP E2E tested** (no live `GEMINI_API_KEY` run) |
| `deepseek` | API Key (`api_key`, env ref) | Analysis output | **Local HTTP & local self-signed HTTPS E2E tested** (no live `DEEPSEEK_API_KEY` run) |

- Interactive official CLI sessions (`codex`, `claude_code`, `grok_build`) prompt the operator to confirm `y` on clean exit before moving the card to `in_review`, otherwise returning it to `todo` with workspace edits preserved. API-key runs (`gemini`, `deepseek`), `mock` runs, and headless `crewline run` move directly to `in_review` on success.
- `codex` and `claude_code` receive the task prompt on launch. Because `grok` documents `-p` as headless, Crewline prints the task prompt into the tmux pane scrollback so you can copy it into Grok's interactive prompt.
- `.crewline/config.toml` stores only environment variable names (such as `api_key_env = "GEMINI_API_KEY"`) and rejects raw secret fields. Crewline redacts configured environment variable values and common key or token patterns from run logs, summaries, and error messages. Frozen Linux builds resolve HTTPS CAs via `SSL_CERT_FILE` or `SSL_CERT_DIR` first, then fall back to the host's system CA bundle (such as `/etc/ssl/certs/ca-certificates.crt`).

Configure named agents and inspect readiness with `crewline doctor`:

```bash
# Official CLI agents (login managed by each vendor's CLI)
crewline agent add codex-dev --provider codex --model gpt-6.1-terra --overwrite
crewline agent add claude-dev --provider claude_code --model claude-sonnet-4-5 --overwrite
crewline agent add grok-dev --provider grok_build --model grok-code-fast-1 --overwrite

# API-key agents (export key in shell; never write secrets into config.toml)
export GEMINI_API_KEY="YOUR_GEMINI_API_KEY"
crewline agent add gemini-analyst --provider gemini --model gemini-2.5-flash --api-key-env GEMINI_API_KEY --overwrite

export DEEPSEEK_API_KEY="YOUR_DEEPSEEK_API_KEY"
crewline agent add deepseek-analyst --provider deepseek --model deepseek-chat --api-key-env DEEPSEEK_API_KEY --overwrite

crewline doctor
```

## CLI & Automation Commands

| Command | Purpose |
|---|---|
| `crewline init [--force]` | Initialize `.crewline/` config, database, logs, and worktrees directory |
| `crewline doctor [--agent NAME] [--json]` | Check CLI, login, and env readiness, agent capabilities, and setup steps |
| `crewline agent list / show / add / remove` | Manage named agent configurations |
| `crewline task create / list [--deleted] / show / assign` | Create (`--agent`), list (`--deleted` for recycle bin), inspect, and assign cards |
| `crewline task delete / restore <id>` | Soft-delete an idle card to the recycle bin or restore with original ID, state, and assignment |
| `crewline run <id>... [--all-todo] [--max-workers N] [--json]` | Run cards in parallel in headless batch mode (`--all-todo` skips deleted cards) |
| `crewline session start / list [--json] / wait [--json] / attach` | Scripted control for detached tmux workbench sessions |
| `crewline logs <id> [--run RUN_ID] [--tail N]` | Read run logs (also readable for deleted cards) |
| `crewline diff <id>` | Show base-to-HEAD commits, staged, unstaged, and untracked changes |
| `crewline review approve <id> [--comment ...] [--cleanup-worktree]` | Approve (`in_review` → `done`). `--cleanup-worktree` refuses dirty or ignored worktrees and keeps the branch. |
| `crewline review reject <id> --comment "..."` | Reject (`in_review` → `todo`) and attach feedback for the next retry |

## Development, Packaging & Scope

- `src/crewline/` keeps `domain/` (models and state machine), `storage/` (SQLite), `workspace/` (Git worktrees and tmux sessions), `security/`, `config/`, `adapters/`, `engine/` (`CrewlineService`), `runtime.py`, and `cli/` decoupled so other frontends or execution backends can be added later.
- Install dev dependencies and run the offline test suite:
  ```bash
  python3 -m pip install -e '.[dev]'
  python3 -m pytest
  ```
- Build a standalone Linux x86_64 archive locally:
  ```bash
  ./scripts/build_linux_dist.sh
  # Specify a Python 3.11+ interpreter or PyPI mirror if needed:
  # ./scripts/build_linux_dist.sh --python /path/to/python3.11 --index-url https://pypi.tuna.tsinghua.edu.cn/simple
  ```
  The build script installs `click>=8.1` and `pyinstaller>=6.9` in a fresh temporary venv (without modifying global Python or Conda) and writes `dist/crewline-<version>-linux-x86_64.tar.gz` plus `.sha256` (both ignored by `.gitignore`).
- End-to-end standalone verification covers Linux `x86_64` (Ubuntu `glibc 2.31`, Git `2.25.1`, tmux `3.0a`) with built-in `mock` agents, the PTY workbench, parallel tmux detach and reconnect under preset `LD_LIBRARY_PATH`, and local self-signed HTTPS CA checks. Linux `arm64`, `musl`/Alpine, other `glibc` versions, macOS, Windows, and real vendor interactive CLIs inside the frozen bundle have not yet been verified.

## License

Crewline is released under the [MIT License](LICENSE).
