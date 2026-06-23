# WodeAgent

<div align="center">

![Python](https://img.shields.io/badge/Python-3.12+-blue.svg)
![License](https://img.shields.io/badge/License-MIT-green.svg)
![Tests](https://img.shields.io/badge/tests-70+_files_240+_cases-brightgreen.svg)

</div>

一个面向学习与实验的 **AI 代码代理框架**，聚焦 **ReAct 循环**、**工具协议**、**上下文工程**、**子代理机制**、**团队协作**、**TUI 交互** 与 **可观测性** 的系统化实践。

> 目标：让"Agent 能做什么"与"Agent 为什么能做"都可追溯、可验证、可扩展。

---

## 核心特性

### 基础引擎
- **ReAct 循环架构**：Thinking → Tool Calling → Observation → Re-thinking
- **多供应商 LLM**：OpenAI / DeepSeek / Qwen / Zhipu / Kimi / Modelscope / SiliconFlow / Ollama / vLLM
- **Function Calling 工具调用**（不依赖 Action 文本解析）
- **统一工具响应协议**：`status/data/text/stats/context/error`

### 工具系统（33 个内置工具）
- **文件**：Read / Write / Edit / MultiEdit / LS / Glob
- **搜索**：Grep
- **系统**：Bash / TodoWrite / AskUser / Skill
- **子代理**：Task (oneshot/persistent/parallel + background)
- **隔离**：EnterWorktree / ExitWorktree
- **计划**：EnterPlanMode / ExitPlanMode
- **模型**：SwitchModel
- **后台**：TaskOutput
- **团队 (16)**：TeamCreate / SendMessage / Status / Delete / Cleanup / Fanout / Collect / List / Retry / Approvals / ApprovePlan / TaskCreate / TaskGet / TaskUpdate / TaskList
- **MCP**：动态外部工具注册

### 高级特性
- **Claude Code 风格 TUI**：Rich Live 流式渲染、@-mention 补全、交互式权限弹窗、状态栏
- **Git Worktree 会话隔离**：EnterWorktree → 隔离工作 → ExitWorktree
- **Plan Mode 只读分析**：先分析后执行，避免盲目修改
- **Background Task 并行执行**：子代理后台运行，主循环不阻塞
- **AgentTeams 团队协作**：多 Agent 消息通信、任务看板、plan approval 闸门
- **Model Profiles + Pointers**：多模型配置、按场景自动选模型、/model 手动切换
- **Output Styles 输出风格**：default / explanatory / learning 三种交互风格
- **VCR 录制回放**：LLM API 调用录制为 fixture，测试零成本确定
- **Hook 生命周期钩子**：PreToolUse / PostToolUse / SessionStart / SessionEnd 自定义脚本
- **Token Budget 追踪**：`/budget` 命令，解析并追踪 token 消耗
- **First-Run Wizard**：首次运行交互式 CLI 配置引导
- **多会话支持**：`/save` 与 `/load` 完整会话快照与恢复

### 上下文工程
- **分层系统提示**：L1 (system+tools) + L2 (CODE_LAW) + runtime blocks
- **历史压缩**：阈值触发 LLM 摘要 + 截断回退
- **工具输出截断**：超限落盘到 `tool-output/`
- **@file 强制读取**：预处理注入 system-reminder
- **轻量熔断**：连续失败工具自动临时禁用
- **Trace 追踪**：JSONL + HTML 双轨日志 + 脱敏
- **会话持久化**：`/save` 与 `/load` 完整快照（含 system、history、teams、read_cache）
- **Skill 技能系统**：Markdown 文件定义，Agent 按需加载
- **Token Budget 追踪**：实时解析 usage、累计统计、`/budget` 查看

### 安全
- **软沙箱权限**：项目外路径需用户确认，敏感目录永拒
- **乐观锁**：Read/Write/Edit 防并发冲突

---

## 快速开始

### 环境要求

- Python 3.12+
- pip / uv

### 安装

```bash
git clone https://github.com/bei-shan/WodeAgent
cd MyCodeAgent
pip install -r requirements.txt
```

### 配置

创建 `.env` 文件：

```bash
# 基础 LLM
LLM_PROVIDER=deepseek
LLM_API_KEY=sk-xxx
LLM_BASE_URL=https://api.deepseek.com
LLM_MODEL_ID=deepseek-v4-pro

# Model Profiles（多模型切换）
MODEL_PROFILES=deepseek,deepseek-chat
MODEL_DEEPSEEK_ID=deepseek-v4-pro
MODEL_DEEPSEEK_CHAT_ID=deepseek-chat
MODEL_POINTER_MAIN=deepseek
MODEL_POINTER_TASK=deepseek-chat
MODEL_POINTER_COMPACT=deepseek-chat

# MCP（可选）
MCP_CONNECT_MODE=manual

# 输出风格（可选）
# AGENT_OUTPUT_STYLE=explanatory
```

### 运行

```bash
python scripts/chat_test_agent.py
```

```bash
# 指定模型
python scripts/chat_test_agent.py --model gpt-4o --provider openai --api-key sk-xxx

# Plan 模式启动
python scripts/chat_test_agent.py --plan
```

---

## Slash Commands

| 命令 | 说明 |
|------|------|
| `/model` | 显示当前模型 + token 统计 |
| `/model <id>` | 切换模型 |
| `/info` | 详细 token 用量 |
| `/plan` | 切换 Plan Mode |
| `/style [name]` | 显示/设置输出风格 |
| `/budget` | 显示 token 预算用量 |
| `/save [path]` | 保存会话快照 |
| `/load [path]` | 加载会话快照 |
| `/team msg <...>` | 发送团队消息 |
| `/team watch <...>` | 监控团队进度 |
| `/delegate <on\|off>` | 切换委托模式 |
| `/help` | 显示帮助 |
| `init` | 生成 code_law.md |
| `exit` / `quit` / `q` | 退出 |

---

## Agent 模式

| 模式 | 触发 | 效果 |
|------|------|------|
| **Plan** | `/plan` / `--plan` / EnterPlanMode | 仅只读工具，产出计划后恢复 |
| **Delegate** | `/delegate on` | 仅团队管理工具，由 teammates 执行 |
| **Worktree** | EnterWorktree | 切换到独立 git worktree 目录 |
| **Background** | Task(run_in_background=true) | 子代理 daemon 线程，主循环继续 |

---

## 项目结构

```
MyCodeAgent/
├── agents/                  # Agent 核心
│   └── codeAgent.py         # CodeAgent (ReAct 循环, 模式管理, 集成点)
├── core/                    # 核心引擎
│   ├── llm.py               # HelloAgentsLLM (多供应商)
│   ├── config.py            # 配置模型
│   ├── agent.py             # Agent 基类
│   ├── background_task.py   # 后台任务运行器
│   ├── budget_tracker.py    # Token 预算追踪
│   ├── model_profiles.py    # 模型 profiles + pointers
│   ├── output_styles.py     # 输出风格管理
│   ├── vcr.py               # LLM API 录制回放
│   ├── hook_system.py       # 生命周期钩子
│   ├── session_store.py     # 会话持久化
│   ├── context_engine/      # 上下文构建、历史管理、压缩、追踪
│   ├── team_engine/         # AgentTeams (manager/supervisor/worker/store)
│   └── worktree/            # Git worktree 管理
├── tools/                   # 工具系统
│   ├── base.py              # Tool 基类 + 协议
│   ├── registry.py          # ToolRegistry
│   ├── permission_gate.py   # 软沙箱
│   ├── circuit_breaker.py   # 熔断器
│   ├── builtin/             # 33 个内置工具
│   └── mcp/                 # MCP 客户端
├── tui/                     # TUI 组件
│   ├── streaming.py         # 流式渲染
│   ├── mention_completer.py # @-mention 补全
│   ├── permission_dialog.py # 权限弹窗
│   └── status_line.py       # 状态栏
├── prompts/                 # LLM 提示词
│   ├── agents_prompts/      # Agent 系统提示 (L1, 子代理)
│   ├── tools_prompts/       # 工具描述 (38 个)
│   └── output_styles/       # 输出风格定义 (default/explanatory/learning)
├── scripts/                 # 入口脚本
│   └── chat_test_agent.py   # 主入口 (TUI + 所有命令)
├── tests/                   # 测试 (77 文件, 841+ 用例)
├── docs/                    # 文档 + 设计文档
├── memory/                  # trace/session/todo 输出
├── skills/                  # 技能定义
├── tool-output/             # 工具输出落盘
└── mcp_servers.json         # MCP 服务器配置
```

---

## 子代理类型

| 类型 | 用途 | 工具 |
|------|------|------|
| `general` | 复杂执行、子任务 | LS/Glob/Grep/Read/TodoWrite |
| `explore` | 代码库扫描、入口点发现 | LS/Glob/Grep/Read/TodoWrite |
| `plan` | 实现步骤分析、依赖评估 | LS/Glob/Grep/Read/TodoWrite |
| `summary` | 压缩长输出、多文件总结 | LS/Glob/Grep/Read/TodoWrite |

模式：`oneshot` (一次性) / `persistent` (持续 teammate) / `parallel` (并发 fanout)

---

## 关键环境变量速查

| 类别 | 变量 | 默认值 |
|------|------|--------|
| LLM | `LLM_PROVIDER` / `LLM_API_KEY` / `LLM_MODEL_ID` / `LLM_BASE_URL` | 需配置 |
| 模型 | `MODEL_PROFILES` / `MODEL_POINTER_MAIN/_TASK/_COMPACT` | 无 |
| 上下文 | `CONTEXT_WINDOW` / `COMPRESSION_THRESHOLD` / `MIN_RETAIN_ROUNDS` | 200000 / 0.8 / 10 |
| 子代理 | `SUBAGENT_MAX_STEPS` | 15 |
| AgentTeams | `ENABLE_AGENT_TEAMS` | false |
| MCP | `MCP_CONNECT_MODE` | manual |
| 安全 | `PERMISSION_SOFT_SANDBOX` | true |
| Worktree | `WORKTREE_STORE_DIR` / `WORKTREE_BASE_REF` | .worktrees / fresh |
| 输出风格 | `AGENT_OUTPUT_STYLE` | default |
| VCR | `VCR_ENABLED` / `VCR_RECORD_MODE` / `VCR_FIXTURE_DIR` | false / new_episodes / tests/fixtures/vcr |
| Trace | `TRACE_ENABLED` / `TRACE_SANITIZE` | true / true |

完整配置参考 `.env.example`。

---

## 测试

```bash
# 全量测试
pytest tests/ -v

# 跳过已知不稳定用例
pytest tests/ --ignore=tests/test_agent_teams_parallel.py \
              --ignore=tests/test_team_worker.py \
              -k "not test_grep_success_no_matches and not test_restore_requeues_running_work_items"

# 仅新功能模块
pytest tests/test_vcr.py tests/test_hook_system.py tests/test_output_styles.py -v

# 单文件
pytest tests/test_vcr.py -v
```

---

## 文档索引

### 协议与架构
- `docs/通用工具响应协议.md` — 工具输出规范
- `docs/上下文工程设计文档.md` — 分层注入、压缩、截断
- `docs/上下文工程与记忆.md` — 会话记忆、快照、文件追踪
- `docs/工具输出截断设计文档.md` — 超限落盘策略
- `docs/TraceLogging设计文档.md` — 双轨追踪
- `docs/PROJECT_OVERVIEW.md` — 完整项目总览
- `docs/DEV_HANDOFF.md` — 开发者交接文档

### 工具设计
- `docs/ReadTool设计文档.md` / `docs/WriteTool设计文档.md` / `docs/EditTool设计文档.md`
- `docs/GlobTool设计文档.md` / `docs/GrepTool设计文档.md` / `docs/LSTool设计文档.md`
- `docs/BashTool设计文档.md` / `docs/TodoWriteTool设计文档.md` / `docs/MultiEditTool设计文档.md`
- `docs/task(subagent)设计文档.md` / `docs/skillTool设计文档.md`

### 功能设计
- `docs/agent_teams/AgentTeams功能设计文档.md` — AgentTeams v2
- `docs/上下文工程与记忆.md` — 会话记忆、压缩、快照、文件追踪
- `docs/design/2026-06-18-worktree-feature-design.md` — Worktree 隔离
- `docs/design/2026-06-18-soft-sandbox-permission-design.md` — 软沙箱
- `docs/design/2026-06-18-kode-agent-learning-plan.md` — Kode-Agent 学习计划
- `docs/design/2026-06-22-output-styles-design.md` — 输出风格
- `docs/design/2026-06-22-vcr-hook-system-design.md` — VCR + Hook
- `docs/design/2026-06-22-codeagent-architecture-refactor.md` — 架构重构
- `docs/design/2026-06-22-token-budget-design.md` — Token 预算追踪
- `docs/design/2026-06-22-first-run-wizard-design.md` — 首次运行向导
- `docs/design/2026-06-22-multi-session-design.md` — 多会话支持
- `docs/design/2026-06-23-coupling-optimization-design.md` — 耦合度优化（工具自动发现 + 统一配置）
- `docs/design/2026-06-23-pi-agent-study-and-optimization-plan.md` — Pi Agent 设计哲学学习 + 优化规划

---

## License

MIT License
