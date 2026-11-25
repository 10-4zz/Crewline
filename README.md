# Crewline v0.1

[English](README.md) · [简体中文](README.zh-CN.md)

A local multi-agent task management CLI: supports creating cards, assigning agents, parallel execution, isolated Git worktrees, real-time secret redaction in logs, and a human review loop.

## Core Features

- **Four-State Task Workflow**: `todo` → `in_progress` → `in_review` → `done`
  - Successful runs automatically move to **in_review**.
  - A reviewer can **approve** (moving to `done`) or **reject with a comment back to `todo`** (the review comment is automatically passed to the agent on the next retry).
  - Failed runs return to **todo**, preserving the failure reason and full run logs.
- **Duplicate-Start Protection & Parallel Git Worktree Isolation**
  - Local SQLite (WAL + `BEGIN IMMEDIATE` + a partial unique index on active runs) ensures the same card cannot be started concurrently more than once.
  - When multiple cards modify the same Git repository in parallel, each card runs in its own `.crewline/worktrees/<task_id>` directory (branch `crewline/<task_id>`) and records its base commit when first branched; subsequent retries/reconnects, main-repo branch switches, or new main-repo commits continue to use the original base commit.
- **Complete Git Review View & Uncommitted Deliverable Protection**
  - `crewline diff <id>` and the workbench shortcut `d` share the same service logic, displaying **committed changes (from base commit to `HEAD`), staged changes, unstaged changes, and new untracked files**. Standard paths, paths with spaces, and Unicode/Chinese paths are supported; untracked file previews are size-capped and never follow symlinks to read files outside the workspace. Legacy cards without a reliable base commit display an explicit notice and still show staged, unstaged, and untracked changes.
  - `crewline review approve --cleanup-worktree` defaults to non-force Git removal: if the worktree contains uncommitted, untracked, or `.gitignore`-ignored local files, cleanup is refused while keeping all files, the branch, the card association, and the `in_review` status intact; clean worktrees are removed cleanly while preserving the result branch. Plain `crewline review approve` and workbench `p` approval keep the worktree by default.
- **Multi-Provider Support & Credential Safety**
  - Configure multiple named agents, selecting each provider's officially supported authentication method:
    - `codex` / `claude_code` / `grok_build`: executed through the user's installed official CLI, with login managed by the official CLI.
    - `gemini` / `deepseek`: invoked via the user's own API key (referenced by environment variable name) in v0.1.
    - `mock`: built-in zero-account mock agent for offline demos and regression testing.
  - The config file stores only credential references (e.g., `api_key_env = "GEMINI_API_KEY"`) and rejects raw secrets; all logs and database writes are redacted automatically.
- **`crewline doctor` Diagnostics**
  - Inspects missing CLIs, login status, or environment variables for every configured agent, provides concrete setup steps, and shows each agent's capabilities (Analysis Output / Edit Workspace Files / Run Tests).

## Architecture

Modules are decoupled by responsibility to make future extensions—such as a desktop UI, SSH remote execution, and a lead agent—straightforward:

```text
src/crewline/
├── domain/      # Pure domain models, capability enums, four-state machine, and typed errors (zero CLI dependencies)
├── storage/     # SQLite persistence, atomic claim locking, run records, and review history
├── workspace/   # WorkspaceBackend protocol + local Git worktree manager (swappable with SSH backend)
├── security/    # Credential reference validation and log/DB secret redactor
├── config/      # .crewline/config.toml reader/writer and default agent templates
├── adapters/    # AgentAdapter interface, registry, and provider adapters (mock/codex/claude/grok/gemini/deepseek)
├── engine/      # CrewlineService core orchestrator (reusable directly by future desktop UI / lead agent)
└── cli/         # Thin CLI command layer and terminal/JSON formatters
```

## Installation

### Option 1: Local Standalone Release Package (Linux x86_64, no Python / pip / sudo)

Runtime requirements:
- **OS & Architecture**: Linux x86_64
- **C Runtime Library**: Target host glibc must be at least the release archive's `build_glibc` (the currently verified release was built on Ubuntu glibc 2.31 and tested only on glibc 2.31; packages built on a newer glibc host are compatible only with that glibc version and newer)
- **External Runtime Dependencies**: `git`, `tmux` (for isolated Git worktrees and detached interactive/background sessions)
- **HTTPS Certificates (API Key mode)**: When calling Gemini/DeepSeek HTTPS APIs, the frozen bundle prioritizes user-configured `SSL_CERT_FILE` / `SSL_CERT_DIR`, and otherwise automatically uses the target Linux host's system CA bundle (such as `/etc/ssl/certs/ca-certificates.crt`) without relying on absolute Conda/Python paths from the build machine.

Once you have the local release archive and matching `.sha256` checksum file:

```bash
# Default install to ~/.local/bin/crewline (runtime stored under ~/.local/share/crewline/releases/...)
./install.sh dist/crewline-0.1.0-linux-x86_64.tar.gz

# Install into a custom or temporary directory (absolute or relative paths; entrypoint is normalized to an absolute path)
./install.sh dist/crewline-0.1.0-linux-x86_64.tar.gz --prefix /tmp/crewline-test
# Or specify individually: --bin-dir <dir> --data-dir <dir> --checksum <file.sha256>
```

Installer behavior:
- Requires no `sudo`, no runtime `pip`, and no system Python, and does not hardcode external download URLs.
- Strictly verifies architecture, glibc (including `build_glibc` in `BUILD_INFO.txt`), Git, tmux, and the SHA-256 checksum before touching any target directory; extracts to a staging directory and runs `--version` smoke validation before performing a single atomic symlink switch on `bin/crewline`, so failures at any step (including an unwritable bin directory) clean up staging files and preserve any existing working installation.

### Option 2: Source Installation (Development Mode, Unchanged)

Requires Python 3.11+, Git, and tmux:

```bash
pip install -e .
# For offline or no-build-isolation environments:
# python3 -m pip install --no-build-isolation --no-deps -e .
```

Verify installation:

```bash
crewline --version
```

### Building the Linux x86_64 Standalone Release from Source

The build script defaults to `python3` on `PATH` (explicitly enforcing `>= 3.11`), installs build dependencies (`click>=8.1` and `pyinstaller>=6.9`, paired with `PYINSTALLER_RESET_ENVIRONMENT=1` for long-lived background sessions) in a fresh temporary venv, collects package code directly from `src/`, leaves the global Conda/Python environment untouched, and writes artifacts to `dist/` (ignored by `.gitignore`):

```bash
./scripts/build_linux_dist.sh
# If default python3 is older than 3.11 or a custom mirror is needed, pass --python and --index-url, e.g.:
# ./scripts/build_linux_dist.sh --python /path/to/python3.11 --index-url https://pypi.tuna.tsinghua.edu.cn/simple
```

Build outputs:
- `dist/crewline-<version>-linux-x86_64.tar.gz` (PyInstaller `onedir` standalone runtime suited for long-lived tmux background sessions)
- `dist/crewline-<version>-linux-x86_64.tar.gz.sha256` (SHA-256 checksum file)

**Standalone Package Verified Scope**:
- Verified: Linux x86_64 (kernel 5.15 / Ubuntu glibc 2.31), Git 2.25.1, tmux 3.0a; built in a fresh temporary venv with Python 3.11.7 + PyInstaller 6.22.3 (`--onedir`). End-to-end verified in a temporary `HOME`, a Python/pip-free `PATH`, and a preset `LD_LIBRARY_PATH` (with strict `git`/`tmux` wrappers verifying original `LD_LIBRARY_PATH` propagation): `install.sh` (including relative `--prefix`, checksum mismatch protection, and unwritable bin-dir protection), `crewline --version`, opening the workbench in a real PTY, starting two mock cards in parallel tmux background sessions, background survival and reconnect after quitting the workbench, viewing logs/diff, approving cards, and validating frozen `SSL_CERT_FILE` / system CA resolution against a local self-signed HTTPS server.
- Not yet verified: Linux arm64, musl Linux (e.g., Alpine), Linux distributions with glibc `< 2.31` or `> 2.31`, macOS / Windows, other tmux versions, and running real multi-vendor interactive CLIs inside the frozen standalone bundle.

## Terminal Workbench

Run `crewline` directly inside your project directory. On first use, press `i` to initialize, `n` to create a card (after entering title and description, optionally pick a configured agent or press Enter to skip; you can still assign/reassign later with `a`), and `s` to start execution. Move between tasks with arrow keys and launch additional cards in parallel. Starting sessions requires [tmux](https://github.com/tmux/tmux/wiki/Getting-Started).

- Press **Enter** to attach to the selected task terminal. Press **Ctrl+b** then **d** to return to the workbench. Press **q** to close the workbench while agents continue running. Run `crewline` again in the same project directory to reconnect.
- **Codex, Claude Code, and Grok Build** run their official interactive CLIs inside isolated terminals so you can handle logins or tool approvals directly. Codex and Claude Code receive the task prompt on startup; Grok Build prints the task prompt into the pane scrollback so you can copy it into its input box. After exiting the official CLI, enter **y** to submit the deliverable for review.
- **Gemini, DeepSeek, and Mock Agents** execute in background mode and move to `in_review` upon success. Use **l** for logs, **d** for diff, **p** to approve, and **r** to reject with a comment. Terminal sessions and task cards track their state independently.
- **Recoverable Card Deletion (Recycle Bin)**: Press **x** and confirm the card ID to move a non-running card into the recycle bin; press **b** to toggle between active cards and the recycle bin. Inside the recycle bin you can read logs (**l**) and diffs (**d**), press **u** to restore the selected card (keeping its original ID, status, assignment, run/review history, and Git worktree/branch), and accidental start, assign, or review actions are blocked.

The terminal and execution host can be accessed over standard SSH. Running `crewline` in the remote project directory lets you reconnect to running tasks on that machine. If a background session disappears unexpectedly, reopening the workbench marks that run as interrupted and preserves the card and workspace for retry.

Scripts can launch the same background sessions with `crewline session start <id>`, inspect them with `crewline session list --json`, wait for completion with `crewline session wait <id> --json`, or attach with `crewline session attach <id>`. Automation can also call `crewline run <id>...` directly for unattended parallel runs.

## Quick Demo (Copy-Paste Ready, No Account Required)

The following commands create a temporary Git repository and walk through **init → `doctor` → create & assign cards → run two cards in parallel (isolated worktrees) → inspect logs & diff → reject with feedback & retry → approve**:

```bash
# 1. Create a demo Git repository and initialize Crewline
rm -rf /tmp/crewline-demo && mkdir -p /tmp/crewline-demo && cd /tmp/crewline-demo
git init
git config user.name "Crewline Demo"
git config user.email "demo@crewline.local"
printf ".crewline/\n" > .gitignore
printf "# Crewline Demo Repo\n" > README.md
git add . && git commit -m "chore: initial commit"

crewline init

# 2. Run doctor to check agent readiness, capabilities, and missing setup steps
crewline doctor

# 3. Create two cards and assign them to zero-account mock agents
crewline task create \
  --title "Write login module design doc" \
  --description "[mock:delay=0.3] [mock:file=docs/auth.md] Draft login module design specification"

crewline task create \
  --title "Add payment module test checklist" \
  --description "[mock:delay=0.3] [mock:file=docs/payment-tests.md] Draft payment regression test checklist" \
  --agent mock-qa

crewline task assign task-001 mock-dev
crewline task list

# 4. Run both cards in parallel (each gets an isolated Git worktree: crewline/task-001 and crewline/task-002)
crewline run task-001 task-002

# 5. Inspect run logs and isolated worktree diff
crewline logs task-001
crewline diff task-001
crewline task list

# 6. Reject task-001 back to todo with a comment, then retry (the agent receives the feedback automatically)
crewline review reject task-001 --comment "Please add token expiration and error code handling details"
crewline task list
crewline run task-001
crewline logs task-001
crewline diff task-001

# 7. Approve both cards to move them to done
crewline review approve task-001 --comment "Details added, approved"
crewline review approve task-002 --comment "Checklist looks complete, approved"
crewline task list
crewline task show task-001
```

## Configuring Real Provider Agents

### 1. Official CLI Providers (Capabilities: Analysis Output, Edit Workspace Files, Run Tests)

- **Codex (`codex`)**: install the official CLI and manage login via `codex login`.
  ```bash
  crewline agent add codex-dev --provider codex --model gpt-6.1-terra --overwrite
  crewline doctor --agent codex-dev
  ```
  > Tip: In container environments that do not support nested sandboxing, you can pass `--extra-arg=-s --extra-arg=danger-full-access` to adjust the Codex CLI sandbox policy.
- **Claude Code (`claude_code`)**: install `claude` and manage login via `claude auth login`.
  ```bash
  crewline agent add claude-dev --provider claude_code --model claude-sonnet-4-5 --overwrite
  ```
- **Grok Build (`grok_build`)**: install `grok` and manage login via `grok login`.
  ```bash
  crewline agent add grok-dev --provider grok_build --model grok-code-fast-1 --overwrite
  ```

### 2. API Key Providers (v0.1 Capability: Analysis Output)

Store only the environment variable name in `config.toml`; never put raw API keys in configuration files:

- **Gemini (`gemini`)**:
  ```bash
  export GEMINI_API_KEY="YOUR_GEMINI_API_KEY"
  crewline agent add gemini-analyst --provider gemini --model gemini-2.5-flash --api-key-env GEMINI_API_KEY --overwrite
  crewline doctor --agent gemini-analyst
  ```
- **DeepSeek (`deepseek`)**:
  ```bash
  export DEEPSEEK_API_KEY="YOUR_DEEPSEEK_API_KEY"
  crewline agent add deepseek-analyst --provider deepseek --model deepseek-chat --api-key-env DEEPSEEK_API_KEY --overwrite
  crewline doctor --agent deepseek-analyst
  ```

## Command Quick Reference

| Command | Description |
|---|---|
| `crewline init [--force]` | Initialize `.crewline/`, default `config.toml`, and SQLite database |
| `crewline doctor [--agent NAME] [--json]` | Check each agent's CLI/login/credential readiness, capabilities, and setup steps |
| `crewline agent list / show / add / remove` | Manage named agent configurations |
| `crewline task create / list [--deleted] / show / assign` | Create cards (`--agent` supported), list (`--deleted` for recycle bin), inspect details, and assign agents |
| `crewline task delete / restore <id>` | Soft-delete cards into the recycle bin and restore with original ID/status/assignment |
| `crewline run <id>... [--all-todo] [--max-workers N]` | Run task cards in parallel (isolated Git worktree per card on the same repo) |
| `crewline logs <id> [--run RUN_ID] [--tail N]` | View redacted execution logs |
| `crewline diff <id>` | View full changes from base commit to current workspace (committed/staged/unstaged/untracked) |
| `crewline review approve <id> [--comment ...] [--cleanup-worktree]` | Approve (`in_review` → `done`); `--cleanup-worktree` safely removes only clean worktrees while keeping the result branch |
| `crewline review reject <id> --comment "..."` | Reject with a comment (`in_review` → `todo`) |

## Provider Adapters & Verification Status

> Note: The standalone Linux x86_64 frozen package above has currently been end-to-end verified only with built-in `mock` agents and a local self-signed HTTPS server. The live `codex` record below comes from an earlier direct-host/source verification run; running real vendor CLIs inside the frozen bundle has not yet been live-tested.

| Provider (`provider`) | Auth Method | Capabilities | Verification Status | Notes |
|---|---|---|---|---|
| `mock` | No account (`none`) | Analysis output, edit workspace files, run tests | **Fully verified** | Verified across the README demo, standalone frozen bundle E2E checks, and full automated test suite |
| `codex` | Official CLI (`official_cli`) | Analysis output, edit workspace files, run tests | **Live E2E verified (source/host run)** | Verified on host `codex-cli 0.160.0` (official ChatGPT login, `gpt-6.1-terra`) across create → run → file edit in isolated Git worktree → `diff` → approve; not yet live-tested inside the standalone frozen bundle |
| `claude_code` | Official CLI (`official_cli`) | Analysis output, edit workspace files, run tests | **Mock-tested only (not live-tested)** | `claude` CLI is not installed in this environment; verified `doctor`, CLI argument assembly, workspace edits, and failure rollback via executable mock CLI |
| `grok_build` | Official CLI (`official_cli`) | Analysis output, edit workspace files, run tests | **Mock-tested only (not live-tested)** | `grok` CLI is not installed in this environment; verified `doctor`, CLI argument assembly, workspace edits, and failure rollback via executable mock CLI |
| `gemini` | API Key (`api_key`, env ref) | Analysis output | **Local HTTP E2E tested (no live public API run)** | `GEMINI_API_KEY` is not configured in this environment; verified `x-goog-api-key` header, key-free URL, JSON parsing, redaction, and state transitions via a local HTTP server |
| `deepseek` | API Key (`api_key`, env ref) | Analysis output | **Local HTTP E2E tested (no live public API run)** | `DEEPSEEK_API_KEY` is not configured in this environment; verified `Authorization: Bearer` header, `/chat/completions` payload/response parsing, redaction, state transitions, and local self-signed HTTPS CA handling |

## Running Automated Tests

```bash
pytest -v
```

## License

Crewline is released under the [MIT License](LICENSE).
