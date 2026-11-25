# Crewline

[English](README.md) · [简体中文](README.zh-CN.md)

Codex 修一个解析器 Bug，Claude Code 同时补回归测试，你在同一个看板里看两边的 diff 并审核。

Crewline 为每张任务卡分配独立的 Git worktree 和终端会话，让多个 AI 编程 Agent 在同一个仓库里并行工作，由你在一处统一查看进度与审核改动。

```text
  ●─●─● Crewline 0.1.0                               （看板界面示意）
  ~/your-project
  待办 0   处理中 1   待审核 1   审核完成 0

  task-001 待审核  codex   修复解析器边界问题
  task-002 处理中  claude  补充回归测试
```

## 安装

需要 **Python 3.11+**、**Git** 和 **tmux**。建议安装到独立虚拟环境，不修改全局 Python 和 `pip`：

```bash
python3 -m venv ~/.crewline-venv
source "$HOME/.crewline-venv/bin/activate"
pip install git+https://github.com/10-4zz/Crewline.git
crewline --version
```

新开终端时，重新执行 `source "$HOME/.crewline-venv/bin/activate"`，或直接调用 `~/.crewline-venv/bin/crewline`。

## 首次上手（无需账号）

内置模拟 Agent（`mock-dev` 和 `mock-qa`）无需任何模型账号，可直接体验多卡并行与审核流程。

1. 进入任意已有至少一次提交的 Git 仓库并启动 Crewline：
   ```bash
   cd /path/to/your-git-repo
   crewline
   ```
2. 按 `i` 初始化项目内的 `.crewline/`（建议将 `.crewline/` 加入 `.gitignore`）。
3. 创建并启动第一张卡：按 `n`，填写标题和描述，输入 `mock-dev` 回车，然后直接按 `s` 启动。
4. 创建并并行启动第二张卡：再按 `n`，填写标题和描述，输入 `mock-qa` 回车，然后按 `s` 启动。
   - 每张卡都在独立 worktree（`.crewline/worktrees/<task_id>`，分支 `crewline/<task_id>`）运行。默认模拟 Agent 会写入示例文件 `crewline_outputs/<task_id>.md`，完成后自动进入**待审核**。
5. 审核结果：
   - 按 `d` 查看完整 diff（涵盖已提交、已暂存、未暂存与未跟踪变更），按 `l` 查看日志。
   - 按 `r` 输入退回意见将卡退回**待办**，再按 `s` 带着意见重试。
   - 按 `p`，输入 `y` 回车确认，将卡片标为**审核完成**。

按 `q` 退出看板后，任务继续在 `tmux` 后台运行，本机或通过 SSH 回到同一目录再次运行 `crewline` 即可重连。非运行中的卡还可按 `x` 移入回收区、按 `b` 查看回收区、按 `u` 恢复。

**审核后的 Git 流程**：审核通过只把卡片标记为完成，不会自动提交或自动合并。请到任务 worktree（`.crewline/worktrees/<task_id>`）检查并提交需要保留的文件，再按常规 Git 流程合并分支或发起 PR。

## 接入真实 Agent

初始化项目时已内置常用 Agent 名称。例如登录 Codex 后检查一次，在看板指派时直接输入 `codex` 即可使用：

```bash
codex login
crewline doctor --agent codex
```

| 内置 Agent | 配置方式 | 能力 |
|---|---|---|
| `codex` | `codex login` | `tmux` 交互 CLI，可分析、改文件、跑测试 |
| `claude` | `claude auth login` | `tmux` 交互 CLI，可分析、改文件、跑测试 |
| `grok` | `grok login` | `tmux` 交互 CLI，可分析、改文件、跑测试 |
| `gemini` | `export GEMINI_API_KEY=...` | 后台 API 调用，v0.1 仅输出分析结果 |
| `deepseek` | `export DEEPSEEK_API_KEY=...` | 后台 API 调用，v0.1 仅输出分析结果 |
| `mock-dev`、`mock-qa` | 无需配置 | 本地模拟，写入 `crewline_outputs/<task_id>.md` |

- 官方交互 CLI 会话（按 `Enter` 进入，`Ctrl+b` 再按 `d` 返回看板）正常退出后需输入 `y` 确认提交审核；`grok` 会把任务说明打印在窗格回滚记录中方便复制。
- API Key 与模拟 Agent 成功后自动进入待审核。`.crewline/config.toml` 只保存环境变量名，不保存明文密钥。

## 命令行入口

看板之外也支持直接用命令行查询与操作：

```bash
crewline --help
crewline doctor
crewline task list
```

## 开源协议

Crewline 采用 [MIT License](LICENSE) 开源协议。
