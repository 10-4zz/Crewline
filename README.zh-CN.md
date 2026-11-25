# Crewline

[English](README.md) · [简体中文](README.zh-CN.md)

Crewline 是一个在同一本地仓库中协调多个 AI 编程 Agent 的工作空间，当前提供终端工作台和 CLI 入口（`v0.1.0`）。

任务卡遵循四态流转：`待办 (todo)` → `处理中 (in_progress)` → `待审核 (in_review)` → `审核完成 (done)`。执行失败和审核退回都会回到 `todo`，并保留失败原因、日志或审核意见供下次重试。

- 每张卡在独立 Git worktree（`.crewline/worktrees/<task_id>`，分支 `crewline/<task_id>`）中运行并记录首次分出时的基准提交。SQLite 原子锁会阻止同一张卡并发重复启动。
- `crewline diff` 与工作台 `d` 展示从基准提交到 `HEAD` 的已提交变更，以及已暂存、未暂存与新增未跟踪文件，并对未跟踪文件做大小截断且不跟随符号链接。`review approve --cleanup-worktree` 会先检查未提交、未跟踪和 `.gitignore` 忽略文件，再执行非强制 `git worktree remove`。若工作区不干净，清理会被拒绝，卡片留在 `in_review` 且文件与分支保持完整。
- 审核人员可将产出通过至 `done`，附带意见退回 `todo` 供下次重试，或将非运行中的卡片移入可恢复回收区并保留日志、历史记录与 Git worktree。
- 可以组合使用官方 CLI（`codex`、`claude_code`、`grok_build`）、API Key 分析（`gemini`、`deepseek`）和无需账号的 `mock` 模拟 Agent。配置文件只保存环境变量名（如 `GEMINI_API_KEY`），不保存明文密钥。

## 快速开始

### 源码安装

需要 **Python 3.11+**、**Git** 与 **tmux**（用于交互与后台会话）。克隆仓库后安装：

```bash
git clone https://github.com/10-4zz/Crewline.git
cd Crewline
python3 -m pip install -e .
crewline --version
```

> 若在离线环境且已预先安装 `setuptools` 与 `click`，可改用 `python3 -m pip install --no-build-isolation --no-deps -e .`。

### 无账号快速演示

以下演示在带 `set -e` 的子 shell 中运行，先创建并进入全新临时目录再初始化 Git 仓库，避免影响当前目录。它完整演示初始化、诊断、两张 mock 卡在独立 worktree 并行运行、查看日志与 diff、退回重试与审核完成：

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

  # 1. 初始化本地状态并检查 Agent
  crewline init
  crewline doctor

  # 2. 创建两张卡并分派给内置模拟 Agent
  crewline task create \
    --title "编写登录模块设计说明" \
    --description "[mock:delay=0.3] [mock:file=docs/auth.md] 编写认证模块设计说明"
  crewline task assign task-001 mock-dev

  crewline task create \
    --title "补充计费回归测试清单" \
    --description "[mock:delay=0.3] [mock:file=docs/billing-tests.md] 编写计费模块回归测试清单" \
    --agent mock-qa

  # 3. 并行运行两张卡（各自使用独立 Git worktree）
  crewline run task-001 task-002

  # 4. 查看运行日志与完整 worktree 变更
  crewline logs task-001
  crewline diff task-001

  # 5. 附带意见退回 task-001，重新运行并复查变更
  crewline review reject task-001 --comment "请补充 Token 过期与错误码处理说明"
  crewline run task-001
  crewline diff task-001

  # 6. 审核通过两张卡，进入审核完成（done）
  crewline review approve task-001 --comment "已补充说明，通过"
  crewline review approve task-002 --comment "测试清单完整，通过"
  crewline task list
  crewline task show task-001
  echo "演示目录: $DEMO_DIR"
)
```

### 使用本地 Linux x86_64 独立包安装

当前仓库仅发布**源码**（尚未发布预构建的公开 GitHub Release 归档）。如果你手头已有通过 `./scripts/build_linux_dist.sh` 在 `dist/` 下本地构建出的 `crewline-<version>-linux-x86_64.tar.gz` 及其 `.sha256` 校验文件，可无需 Python、`pip` 或 `sudo` 直接安装：

```bash
# 默认安装到 ~/.local/bin/crewline（运行时位于 ~/.local/share/crewline/releases/）
./install.sh dist/crewline-0.1.0-linux-x86_64.tar.gz
export PATH="$HOME/.local/bin:$PATH"
crewline --version

# 也可安装到自定义或临时目录（支持绝对或相对路径）
./install.sh dist/crewline-0.1.0-linux-x86_64.tar.gz --prefix /tmp/crewline-test
# 可选参数：--bin-dir <dir> --data-dir <dir> --checksum <file.sha256>
```

- 目标主机需要 Linux `x86_64`、系统已安装 `git` 和 `tmux`，且 `glibc` 至少为 `2.31` 并至少达到归档内的 `build_glibc`。当前实测环境为 Ubuntu `glibc 2.31`，在更高版本 `glibc` 主机构建的归档仅兼容该版本及以上 `glibc`。
- `install.sh` 在修改任何目标路径前会检查架构、`glibc`、`git`、`tmux` 与 SHA-256，随后在暂存目录对新二进制执行 `--version` 自检，再对 `bin/crewline` 执行单次原子符号链接替换。若中途出错，会清理暂存文件并保持原有入口不变。

## 终端工作台

在项目目录直接运行 `crewline` 即可打开交互式任务看板（启动会话需安装 `tmux`）。

| 快捷键 | 说明 |
|---|---|
| `i` | 初始化 `.crewline/` 工作区（首次未初始化时） |
| `↑` / `↓` 或 `k` / `j` | 切换选中任务卡 |
| `n` | 建卡（填写标题与描述后可选指派已配置 Agent，或回车跳过） |
| `a` | 为选中卡指派或改派 Agent |
| `s` | 在独立后台 tmux 会话中启动选中的待办卡 |
| `Enter` | 进入选中卡的活动终端会话 |
| `Ctrl+b` 后按 `d` | 断开任务终端并返回工作台（Agent 继续运行） |
| `l` / `d` | 查看运行日志或完整 Git diff（只读，回收区同样可用） |
| `p` / `r` | 审核通过（`in_review` → `done`，保留 worktree）或附带意见退回（`in_review` → `todo`） |
| `x` | 将非运行中的卡片移入回收区（显示卡 ID 并确认） |
| `b` | 在当前卡片列表与回收区之间切换 |
| `u` | 从回收区恢复选中卡（保留原 ID、原状态、原分派、历史记录与 worktree） |
| `q` | 退出工作台（后台会话继续运行） |

- 按 `q` 只关闭看板界面，运行中的任务会继续留在后台 tmux 会话中。在同一项目目录（本机或通过普通 SSH 登录该机器后）再次运行 `crewline` 即可重连。若后台会话异常退出，重开工作台会将该次运行标为中断并保留卡片与工作区供重试。
- 在回收区浏览时禁止启动、指派、审核与新建操作，防止误触。

## 厂商适配与凭证安全

| 厂商 (`provider`) | 认证方式 | 能力 | 当前实测范围 |
|---|---|---|---|
| `mock` | 无需账号 (`none`) | 输出分析、修改文件、运行测试 | **已实测**，覆盖完整 CLI 工作流、自动化测试与 Linux x86_64 冻结包并行验收 |
| `codex` | 官方 CLI (`official_cli`) | 输出分析、修改文件、运行测试 | **先前源码/宿主机真机实测**（`codex-cli 0.160.0`、ChatGPT 登录、`gpt-6.1-terra`）。冻结包内尚未真机实测。 |
| `claude_code` | 官方 CLI (`official_cli`) | 输出分析、修改文件、运行测试 | **仅模拟 CLI 测试**（当前主机未安装 `claude`） |
| `grok_build` | 官方 CLI (`official_cli`) | 输出分析、修改文件、运行测试 | **仅模拟 CLI 测试**（当前主机未安装 `grok`） |
| `gemini` | API Key (`api_key`, env 引用) | 输出分析结果 | **仅本地 HTTP 端到端测试**（无公网 `GEMINI_API_KEY` 真机实测） |
| `deepseek` | API Key (`api_key`, env 引用) | 输出分析结果 | **仅本地 HTTP 与本地自签名 HTTPS 测试**（无公网 `DEEPSEEK_API_KEY` 真机实测） |

- 官方交互 CLI 会话（`codex`、`claude_code`、`grok_build`）正常退出后会提示操作员输入 `y` 确认提交进入 `in_review`，否则退回 `todo` 并保留工作区改动。API Key 任务（`gemini`、`deepseek`）、`mock` 任务以及无头批处理 `crewline run` 在执行成功时自动进入 `in_review`。
- `codex` 与 `claude_code` 启动时直接收到任务说明。因为 `grok` 将 `-p` 定义为无头模式，工作台会把任务说明打印在窗格回滚记录中，供复制到其输入框。
- `.crewline/config.toml` 只保存环境变量名（如 `api_key_env = "GEMINI_API_KEY"`）并拒绝明文密钥字段。Crewline 会对运行日志、运行摘要与错误消息中的已配置环境变量值及常见密钥/令牌模式执行脱敏。Linux 冻结包发起 HTTPS API 请求时优先使用 `SSL_CERT_FILE` 或 `SSL_CERT_DIR`，未设置时使用目标主机系统 CA（如 `/etc/ssl/certs/ca-certificates.crt`）。

配置具名 Agent 并使用 `crewline doctor` 检查就绪状态与能力：

```bash
# 官方 CLI 类（由各厂商官方 CLI 管理登录）
crewline agent add codex-dev --provider codex --model gpt-6.1-terra --overwrite
crewline agent add claude-dev --provider claude_code --model claude-sonnet-4-5 --overwrite
crewline agent add grok-dev --provider grok_build --model grok-code-fast-1 --overwrite

# API Key 类（在终端导出环境变量，切勿写入 config.toml）
export GEMINI_API_KEY="你的_Gemini_API_Key"
crewline agent add gemini-analyst --provider gemini --model gemini-2.5-flash --api-key-env GEMINI_API_KEY --overwrite

export DEEPSEEK_API_KEY="你的_DeepSeek_API_Key"
crewline agent add deepseek-analyst --provider deepseek --model deepseek-chat --api-key-env DEEPSEEK_API_KEY --overwrite

crewline doctor
```

## 常用 CLI 与脚本入口

| 命令 | 说明 |
|---|---|
| `crewline init [--force]` | 初始化 `.crewline/` 配置、数据库、日志与 worktrees 目录 |
| `crewline doctor [--agent NAME] [--json]` | 检查各 Agent 的 CLI、登录和环境变量就绪状态、能力与配置步骤 |
| `crewline agent list / show / add / remove` | 管理具名 Agent 配置 |
| `crewline task create / list [--deleted] / show / assign` | 建卡（支持 `--agent`）、列表（`--deleted` 查看回收区）、详情与分派 |
| `crewline task delete / restore <id>` | 将非运行中的卡片移入回收区，或按原 ID、状态与分派恢复 |
| `crewline run <id>... [--all-todo] [--max-workers N] [--json]` | 无头批处理并行运行任务卡（`--all-todo` 自动跳过已删除卡） |
| `crewline session start / list [--json] / wait [--json] / attach` | 脚本化控制工作台后台 tmux 会话 |
| `crewline logs <id> [--run RUN_ID] [--tail N]` | 查看运行日志（已删除卡仍可只读查看） |
| `crewline diff <id>` | 查看基准提交到当前工作区的已提交、已暂存、未暂存与未跟踪变更 |
| `crewline review approve <id> [--comment ...] [--cleanup-worktree]` | 审核通过（`in_review` → `done`）。`--cleanup-worktree` 拒绝脏目录或忽略文件并保留结果分支。 |
| `crewline review reject <id> --comment "..."` | 附带意见退回（`in_review` → `todo`），意见自动带入下次重试 |

## 开发、构建与当前范围

- `src/crewline/` 将 `domain/`（领域模型与状态机）、`storage/`（SQLite）、`workspace/`（Git worktree 与 tmux 会话）、`security/`、`config/`、`adapters/`、`engine/`（`CrewlineService`）、`runtime.py` 与 `cli/` 解耦，便于后续扩展其他前端或执行后端。
- 安装开发依赖并运行离线测试：
  ```bash
  python3 -m pip install -e '.[dev]'
  python3 -m pytest
  ```
- 本地构建 Linux x86_64 独立发行包：
  ```bash
  ./scripts/build_linux_dist.sh
  # 如需指定 Python 3.11+ 或镜像源：
  # ./scripts/build_linux_dist.sh --python /path/to/python3.11 --index-url https://pypi.tuna.tsinghua.edu.cn/simple
  ```
  脚本在全新临时 venv 内安装 `click>=8.1` 与 `pyinstaller>=6.9`（不修改全局 Python 或 Conda），于 `dist/` 生成 `crewline-<version>-linux-x86_64.tar.gz` 与 `.sha256`（均由 `.gitignore` 忽略）。
- 独立发行包当前仅在 Linux `x86_64`（Ubuntu `glibc 2.31`、Git `2.25.1`、tmux `3.0a`）完成端到端验收，涵盖内置 `mock` Agent、PTY 工作台、预设 `LD_LIBRARY_PATH` 下并行 tmux 断开与重连，以及本地自签名 HTTPS CA 校验。Linux `arm64`、`musl`/Alpine、其他 `glibc` 版本、macOS、Windows 以及冻结包内真实厂商交互 CLI 均尚未实测。

## 开源协议

Crewline 采用 [MIT License](LICENSE) 开源协议。
