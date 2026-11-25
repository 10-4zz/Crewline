# Crewline v0.1

[English](README.md) · [简体中文](README.zh-CN.md)

本地运行的多 Agent 任务管理 CLI：支持建卡、分派、并行执行、独立 Git worktree 隔离、实时日志脱敏与人工审核闭环。

## 核心特性

- **四态任务流**：`待办 (todo)` → `处理中 (in_progress)` → `待审核 (in_review)` → `审核完成 (done)`
  - 执行成功自动进入 **待审核**。
  - 人工可 **通过**（进入审核完成），或 **附带意见退回待办**（下次重试自动把退回意见传给 Agent）。
  - 执行失败自动退回 **待办**，保留失败原因与完整运行日志。
- **防重复启动与并行 worktree 隔离**
  - 本地 SQLite（WAL + `BEGIN IMMEDIATE` + 运行中部分唯一索引）确保同一张卡不会被并发重复启动。
  - 多张卡并行修改同一个 Git 仓库时，每张卡自动在 `.crewline/worktrees/<task_id>`（分支 `crewline/<task_id>`）独立执行，并记录首次分出时的基准提交；后续同卡重试/重连或主仓库切换分支、新增提交时仍沿用原基准。
- **完整 Git 审核视图与未提交成果保护**
  - `crewline diff <id>` 与工作台快捷键 `d` 共用同一服务逻辑，完整展示从基准提交到当前工作区的 **已提交变更、已暂存变更、未暂存变更与新增未跟踪文件**；支持普通路径、含空格和中文路径，对未跟踪文件做大小截断且不跟随符号链接读取工作区外文件。旧版本缺少基准提交的任务卡会明确提示，并继续展示已暂存、未暂存与未跟踪变更。
  - `crewline review approve --cleanup-worktree` 默认使用 Git 非强制删除：若 worktree 存在未提交、未跟踪或被 `.gitignore` 忽略的本地文件，会拒绝清理并保留全部文件、分支、卡片关联与待审核状态；干净 worktree 会正常移除目录并保留结果分支。普通 `crewline review approve` 与工作台 `p` 审核通过均默认保留 worktree。
- **多厂商与凭证安全**
  - 支持多个具名 Agent，按厂商选择官方认证方式：
    - `codex` / `claude_code` / `grok_build`：通过用户安装的官方 CLI 执行，由官方 CLI 管理登录。
    - `gemini` / `deepseek`：首版通过用户自己的 API Key（环境变量引用）调用。
    - `mock`：内置无需账号的模拟 Agent，可直接离线演示和回归测试。
  - 配置文件只保存凭证引用（例如 `api_key_env = "GEMINI_API_KEY"`），拒绝明文密钥；所有日志和数据库写入前均自动脱敏。
- **`crewline doctor` 诊断**
  - 一键检查各 Agent 缺少的 CLI、登录状态或环境变量，给出具体配置步骤，并展示各 Agent 能力（输出分析结果 / 修改工作区文件 / 运行测试）。

## 架构分层

代码按职责解耦，便于后续扩展桌面 UI、SSH 远程执行与领队 Agent：

```text
src/crewline/
├── domain/      # 纯领域模型、能力枚举、四态状态机与错误类型（零 CLI 依赖）
├── storage/     # SQLite 持久化、原子抢占锁、运行记录与审核历史
├── workspace/   # WorkspaceBackend 协议 + 本地 Git worktree 管理（可替换为 SSH 实现）
├── security/    # 凭证引用校验与日志/数据库脱敏器
├── config/      # .crewline/config.toml 读写与默认 Agent 模板
├── adapters/    # AgentAdapter 接口、注册表与各厂商适配器（mock/codex/claude/grok/gemini/deepseek）
├── engine/      # CrewlineService 核心编排服务（可供未来桌面 UI / 领队 Agent 直接调用）
└── cli/         # 薄 CLI 命令层与终端/JSON 格式化输出
```

## 安装

### 方式一：本地独立发行包安装（Linux x86_64，无需 Python / pip / sudo）

运行系统要求：
- **操作系统与架构**：Linux x86_64
- **C 运行库**：目标主机 glibc 版本需不低于该发行包构建主机的 `build_glibc`（当前实测构建包基于 Ubuntu glibc 2.31 构建，仅在 glibc 2.31 环境实测；若在更高版本 glibc 主机构建，产物仅兼容该构建版本及以上的 glibc）
- **外部运行依赖**：`git`、`tmux`（用于独立 worktree 与后台交互会话）
- **HTTPS 证书（API Key 模式）**：冻结发行包调用 Gemini/DeepSeek HTTPS API 时优先使用用户设置的 `SSL_CERT_FILE` / `SSL_CERT_DIR`，未设置时自动使用目标 Linux 主机的系统 CA 证书包（如 `/etc/ssl/certs/ca-certificates.crt`），不依赖构建机上的 Conda/Python 绝对路径。

拿到本地发行包及同名 `.sha256` 校验文件后执行：

```bash
# 默认安装到 ~/.local/bin/crewline（运行时位于 ~/.local/share/crewline/releases/...）
./install.sh dist/crewline-0.1.0-linux-x86_64.tar.gz

# 也可安装到自定义或临时目录（支持绝对或相对路径，入口会自动归一为绝对路径）
./install.sh dist/crewline-0.1.0-linux-x86_64.tar.gz --prefix /tmp/crewline-test
# 或分别指定：--bin-dir <dir> --data-dir <dir> --checksum <file.sha256>
```

安装器特性：
- 全程无需 `sudo`，不调用 `pip`，不依赖系统 Python，且不硬编码外部下载地址。
- 安装前强制校验架构、glibc（含校验包内 `BUILD_INFO.txt` 的 `build_glibc`）、Git、tmux 与 SHA-256 校验和；解压后先在暂存目录对新二进制执行 `--version` 自检，通过后将入口 `bin/crewline` 直接原子切换到已验证的版本目录，任意步骤失败（包括入口目录不可写）都会清理暂存目录且不破坏已有可用安装。

### 方式二：源码安装（开发模式，原样可用）

需要 Python 3.11+、Git 与 tmux：

```bash
pip install -e .
# 离线或无构建隔离环境可用：
# python3 -m pip install --no-build-isolation --no-deps -e .
```

验证安装：

```bash
crewline --version
```

### 从源码构建 Linux x86_64 独立发行包

构建脚本默认使用 `PATH` 中的 `python3`（显式校验 `>= 3.11`），在全新临时 venv 中安装构建依赖（`click>=8.1` 与 `pyinstaller>=6.9`，配合 `PYINSTALLER_RESET_ENVIRONMENT=1` 支持长期后台会话），直接从 `src/` 收集代码，不会修改全局 Conda/Python 环境，产物输出到 `dist/`（已由 `.gitignore` 忽略）：

```bash
./scripts/build_linux_dist.sh
# 若系统默认 python3 低于 3.11 或需指定镜像源，可显式传入 --python 与 --index-url，例如：
# ./scripts/build_linux_dist.sh --python /path/to/python3.11 --index-url https://pypi.tuna.tsinghua.edu.cn/simple
```

构建产物：
- `dist/crewline-<version>-linux-x86_64.tar.gz`（PyInstaller `onedir` 独立运行时，适配长期存活的 tmux 后台会话）
- `dist/crewline-<version>-linux-x86_64.tar.gz.sha256`（SHA-256 校验文件）

**发行包实际验证范围**：
- 已实测：Linux x86_64（kernel 5.15 / Ubuntu glibc 2.31）、Git 2.25.1、tmux 3.0a；基于 Python 3.11.7 + PyInstaller 6.22.3 (`--onedir`) 在全新临时 venv 中构建；在临时 `HOME`、无 `python`/`python3`/`pip` 的 `PATH` 以及预设 `LD_LIBRARY_PATH`（配 `git`/`tmux` 包装器校验原始 `LD_LIBRARY_PATH` 透传）环境中完整通过 `install.sh`（含相对 `--prefix`、校验失败与入口不可写失败保护）、`crewline --version`、真实 PTY 打开工作台、并行启动两张模拟卡 tmux 后台会话、退出工作台后后台存活与重连、查看日志/diff、审核通过，以及使用本地自签名 HTTPS 服务验证冻结版 `SSL_CERT_FILE` 与系统 CA 路径解析。
- 未实测：Linux arm64、musl Linux（如 Alpine）、glibc < 2.31 或高于 2.31 的其他 Linux 发行版、macOS / Windows，以及不同版本 tmux / 真实多厂商交互 CLI 在冻结包内的兼容性。

## 终端工作台

在项目目录直接运行 `crewline`。首次使用时按 `i` 初始化，按 `n` 建卡（填写标题与描述后可直接选择已配置 Agent 或回车跳过，之后仍可按 `a` 指派/改派）、按 `s` 启动。用方向键在任务间移动，选中其他卡后可以继续启动并行任务。启动会话需要安装 [tmux](https://github.com/tmux/tmux/wiki/Getting-Started)。

- 按 **Enter** 进入当前任务终端。按 **Ctrl+b** 再按 **d** 返回工作台。按 **q** 关闭工作台，Agent 继续运行。在相同项目目录再次运行 `crewline` 可重连。
- **Codex、Claude Code 和 Grok Build** 在独立终端里运行官方交互 CLI。需要登录或批准工具操作时可以进入终端处理。Codex 和 Claude Code 启动时收到任务说明。Grok Build 的任务说明显示在窗格回滚记录中，可以复制到输入框。退出官方 CLI 后，输入 **y** 会将产出提交审核。
- **Gemini、DeepSeek 和模拟 Agent** 以后台模式执行，成功时进入待审核。用 **l** 查看日志、**d** 查看变更、**p** 通过审核、**r** 附带意见退回。终端会话和任务卡各自记录状态。
- **卡片可恢复删除（回收区）**：按 **x** 确认卡 ID 后将非运行中的卡片移入回收区；按 **b** 在当前卡片与回收区之间切换；在回收区可按 **l**/**d** 只读查看日志与变更，按 **u** 恢复选中卡（保留原 ID、原状态、原分派、运行/审核历史与 Git worktree/分支），且回收区禁止误启动、指派或审核。

终端和执行所在的机器可以通过普通 SSH 访问。在远程项目目录运行 `crewline`，可在同一机器上重新连接该任务。后台会话消失后，再打开工作台会把对应的运行标为中断，保留任务和工作区供重试。

脚本可以用 `crewline session start <id>` 启动同样的后台会话，用 `crewline session list --json` 查看会话，用 `crewline session wait <id> --json` 等待该次运行，或用 `crewline session attach <id>` 重连。自动化也可以直接调用 `crewline run <id>...` 并行运行无人值守的 Agent。

## 快速演示（可直接复制运行，无需账号）

以下命令会创建一个临时 Git 仓库，演示 **初始化 → `doctor` 诊断 → 建卡分派 → 并行运行两张卡（独立 worktree）→ 查看日志与变更 → 附带意见退回重试 → 审核通过**：

```bash
# 1. 创建演示 Git 仓库并初始化 Crewline
rm -rf /tmp/crewline-demo && mkdir -p /tmp/crewline-demo && cd /tmp/crewline-demo
git init
git config user.name "Crewline Demo"
git config user.email "demo@crewline.local"
printf ".crewline/\n" > .gitignore
printf "# Crewline Demo Repo\n" > README.md
git add . && git commit -m "chore: initial commit"

crewline init

# 2. 运行 doctor 查看所有 Agent 状态、能力与缺失配置指引
crewline doctor

# 3. 创建两张卡，并分派给无需账号的模拟 Agent
crewline task create \
  --title "实现登录模块文档" \
  --description "[mock:delay=0.3] [mock:file=docs/auth.md] 编写登录模块设计说明"

crewline task create \
  --title "补充支付模块测试清单" \
  --description "[mock:delay=0.3] [mock:file=docs/payment-tests.md] 编写支付模块回归测试清单" \
  --agent mock-qa

crewline task assign task-001 mock-dev
crewline task list

# 4. 并行运行两张卡（各自使用独立 Git worktree：crewline/task-001 与 crewline/task-002）
crewline run task-001 task-002

# 5. 查看运行日志与独立 worktree 变更
crewline logs task-001
crewline diff task-001
crewline task list

# 6. 附带意见退回 task-001 到待办，然后重新运行（Agent 会自动收到退回意见）
crewline review reject task-001 --comment "请补充 Token 过期与错误码处理说明"
crewline task list
crewline run task-001
crewline logs task-001
crewline diff task-001

# 7. 审核通过两张卡，进入审核完成（done）
crewline review approve task-001 --comment "已补充说明，审核通过"
crewline review approve task-002 --comment "测试清单完整，审核通过"
crewline task list
crewline task show task-001
```

## 配置真实厂商 Agent

### 1. 官方 CLI 类（支持：输出分析结果、修改工作区文件、运行测试）

- **Codex (`codex`)**：由用户安装并运行 `codex login` 管理登录。
  ```bash
  crewline agent add codex-dev --provider codex --model gpt-6.1-terra --overwrite
  crewline doctor --agent codex-dev
  ```
  > 提示：在不支持嵌套沙箱的容器环境中，可通过 `--extra-arg=-s --extra-arg=danger-full-access` 调整 Codex CLI 的沙箱参数。
- **Claude Code (`claude_code`)**：由用户安装 `claude` 并运行 `claude auth login` 管理登录。
  ```bash
  crewline agent add claude-dev --provider claude_code --model claude-sonnet-4-5 --overwrite
  ```
- **Grok Build (`grok_build`)**：由用户安装 `grok` 并运行 `grok login` 管理登录。
  ```bash
  crewline agent add grok-dev --provider grok_build --model grok-code-fast-1 --overwrite
  ```

### 2. API Key 类（首版能力：输出分析结果）

配置中只保存环境变量名，切勿将真实密钥写入 `config.toml`：

- **Gemini (`gemini`)**：
  ```bash
  export GEMINI_API_KEY="你的_Gemini_API_Key"
  crewline agent add gemini-analyst --provider gemini --model gemini-2.5-flash --api-key-env GEMINI_API_KEY --overwrite
  crewline doctor --agent gemini-analyst
  ```
- **DeepSeek (`deepseek`)**：
  ```bash
  export DEEPSEEK_API_KEY="你的_DeepSeek_API_Key"
  crewline agent add deepseek-analyst --provider deepseek --model deepseek-chat --api-key-env DEEPSEEK_API_KEY --overwrite
  crewline doctor --agent deepseek-analyst
  ```

## 常用命令速查

| 命令 | 说明 |
|---|---|
| `crewline init [--force]` | 初始化 `.crewline/` 目录、默认 `config.toml` 与 SQLite 数据库 |
| `crewline doctor [--agent NAME] [--json]` | 检查各 Agent 的 CLI/登录/凭证状态、能力与配置步骤 |
| `crewline agent list / show / add / remove` | 管理具名 Agent 配置 |
| `crewline task create / list [--deleted] / show / assign` | 建卡（支持 `--agent`）、列表（`--deleted` 查看回收区）、详情与分派 |
| `crewline task delete / restore <id>` | 可恢复删除任务卡（移入回收区）与按原 ID/状态/分派恢复 |
| `crewline run <id>... [--all-todo] [--max-workers N]` | 并行运行任务卡（同仓库自动独立 Git worktree） |
| `crewline logs <id> [--run RUN_ID] [--tail N]` | 查看脱敏后的运行日志 |
| `crewline diff <id>` | 查看任务卡从基准提交到当前工作区的完整变更（已提交/已暂存/未暂存/未跟踪） |
| `crewline review approve <id> [--comment ...] [--cleanup-worktree]` | 审核通过（待审核 → 审核完成）；`--cleanup-worktree` 仅非强制清理干净 worktree 并保留结果分支 |
| `crewline review reject <id> --comment "..."` | 附带意见退回（待审核 → 待办） |

## 厂商适配与实测状态

| 厂商 (`provider`) | 认证方式 | 能力 | 当前实测状态 | 说明 |
|---|---|---|---|---|
| `mock` | 无需账号 (`none`) | 输出分析结果、修改工作区文件、运行测试 | **已完整实测** | 完整覆盖 README 演示流程与全部自动化测试 |
| `codex` | 官方 CLI (`official_cli`) | 输出分析结果、修改工作区文件、运行测试 | **已真机端到端实测** | 基于本机 `codex-cli 0.160.0`（ChatGPT 官方登录，`gpt-6.1-terra`）在独立 Git worktree 中完成真实建卡→运行→创建文件→`diff`→审核通过闭环 |
| `claude_code` | 官方 CLI (`official_cli`) | 输出分析结果、修改工作区文件、运行测试 | **模拟测试通过（未真机实测）** | 当前环境未安装 `claude` CLI；已通过模拟可执行 CLI 验证 `doctor`、参数组装、文件修改与失败回退 |
| `grok_build` | 官方 CLI (`official_cli`) | 输出分析结果、修改工作区文件、运行测试 | **模拟测试通过（未真机实测）** | 当前环境未安装 `grok` CLI；已通过模拟可执行 CLI 验证 `doctor`、参数组装、文件修改与失败回退 |
| `gemini` | API Key (`api_key`, env 引用) | 输出分析结果 | **本地 HTTP 模拟端到端测试通过（未公网真机实测）** | 当前环境未配置 `GEMINI_API_KEY`；已通过本地真实 HTTP 服务验证 `x-goog-api-key` 请求头、URL 不含密钥、JSON 响应解析、脱敏与状态流转 |
| `deepseek` | API Key (`api_key`, env 引用) | 输出分析结果 | **本地 HTTP 模拟端到端测试通过（未公网真机实测）** | 当前环境未配置 `DEEPSEEK_API_KEY`；已通过本地真实 HTTP 服务验证 `Authorization: Bearer` 请求头、`/chat/completions` 请求与响应解析、脱敏与状态流转 |

## 运行自动化测试

```bash
pytest -v
```

## 开源协议

Crewline 采用 [MIT License](LICENSE) 开源协议。
