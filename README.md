# MyCodeAgent (WodeAgent)

[![Python](https://img.shields.io/badge/Python-3.12+-blue.svg)]()
[![Tests](https://img.shields.io/badge/tests-79%20files%20%7C%20936%20cases-brightgreen.svg)]()
[![Tools](https://img.shields.io/badge/builtin%20tools-33-orange.svg)]()
[![License](https://img.shields.io/badge/license-MIT-lightgrey.svg)]()

> 一个用来"边写边学"的 AI Code Agent 框架 —— 围绕 ReAct 循环、可组合 Feature、上下文工程、子代理 / 多代理团队、TUI 与 Web 桌面端、可观测 Trace 一并打造的实验场。

---

## 最近优化 (2026 Q2)

- **agent-runtime-decouple** ：抽出 `core/runtime/SessionController` + `AgentEvent / EventSink`，CodeAgent 与任何 UI（TUI / Web）通过线程安全事件队列彻底解耦。
- **Web 桌面端**：`desktop/service/` (FastAPI, 40 REST + 1 WebSocket) + `desktop/web/` (Vite + TypeScript + Tailwind) 提供完整图形界面、会话流、文件管理、Skill / MCP / Hooks 配置。
- **Hooks 管理**：`.mycode/hooks.json` 生命周期钩子，可在 Web UI 中直接查看 / 编辑。
- **两层 Skill 系统**：`skills/`（内置、随仓库分发）与 `.mycodeagent/skills/`（运行时可写、覆盖内置），首次运行自动 seed。
- **会话树 (v2)**：JSONL 树形存储 + `/tree` / `/fork` / `/thinking` 等命令，支持回溯、分叉、思考等级追踪。
- **Team Engine 生产硬化**：TeamManager 单一所有权、worktree 重绑、MessageRouter / ApprovalService 文件级持久化、Worker LLM 重试 + 心跳 sweep。
- **LLM Streaming**：`HelloAgentsLLM.stream_raw()` 接入主 ReAct 循环，TUI Rich Live + Web WebSocket 实时渲染。

---

## 1. 项目简介

MyCodeAgent 是一个用 Python 实现的 AI Code Agent 框架，目标是把一个"能用的 Coding Agent"完整拆解成可组合、可观测、可测试的最小单元。

- **语言**：Python 3.12+
- **LLM 供应商**：OpenAI / DeepSeek / Qwen (DashScope) / Zhipu (GLM) / Kimi (Moonshot) / Modelscope / SiliconFlow / Ollama / vLLM
- **入口形态**：交互式 TUI（prompt_toolkit + Rich）、Web 桌面端（FastAPI + Vite）
- **存储**：JSON 快照 + JSONL 会话树 + JSONL Trace
- **可组合性**：11 个内建 AgentFeature + 插件目录（`.mycode/plugins/`）+ 33 个内建工具 + MCP 外部工具
- **测试**：79 个 `test_*.py` 文件，约 936 个测试用例（pytest）

---

## 2. 核心特性

### 基础引擎
- `agents/codeAgent.py` 实现 ReAct 主循环，构造时依次执行 `_init_core → collect_all_features → init() → _init_tools → post_init()`。
- 每轮 LLM 调用支持 streaming（`LLM_STREAMING=true` 默认开启），失败时回退到非流式 `invoke_raw`。
- 内建 `BudgetTracker` / `_total_usage_tokens` 统计 token 用量。

### 工具系统（33 个内建工具）
| 分类 | 数量 | 工具 |
|---|---|---|
| 文件操作 | 6 | Read / Write / Edit / MultiEdit / LS / Glob |
| 搜索 | 1 | Grep（优先 ripgrep，回退 Python）|
| 系统 | 4 | Bash / TodoWrite / AskUser / Skill |
| 子代理 | 2 | Task / TaskOutput |
| Worktree | 2 | EnterWorktree / ExitWorktree |
| Plan | 2 | EnterPlanMode / ExitPlanMode |
| 模型 | 1 | SwitchModel |
| 团队 | 15 | SendMessage + 14 个 team_* |

- `ToolRegistry` 统一生成 OpenAI function-calling schema、归一化 Universal Tool Response、自动注入 `expected_mtime_ms/size_bytes` 做乐观锁。
- `CircuitBreaker` 按工具维度的 CLOSED / OPEN / HALF_OPEN 熔断，模型层错误（INVALID_PARAM 等）不计失败。
- `tools/mcp/` 提供 MCP 外部工具集成（stdio / streamable-HTTP，三种连接模式：startup / manual / disabled）。

### Web 桌面端 (`desktop/`)
- `desktop/service/app.py` 暴露 **40 个 REST 端点 + 1 个 WebSocket**，分组：Health / Sessions / Messages / Permissions & AskUser / Session Config / Agent Teams / Files / Models & Tools / Skills / MCP Servers / Hooks。
- `desktop/web/` 是 Vite + TypeScript + Tailwind 前端工程，构建产物供桌面端加载。
- WebSocket `/api/sessions/{sid}/stream` 转发 12 种事件（8 个 core + 4 个 session 层），payload 经 `_sanitize_payload` 脱敏与截断。

### 运行时与事件系统 (`core/runtime/`)
- `SessionController` 用纯标准库（`queue` / `threading` / `uuid`）管理 `dict[str, AgentSession]`，提供 `create_session / get_session / delete_session / list_sessions`。
- `AgentSession` 在独立守护线程上跑 `agent.run()`，共享 `queue.Queue[AgentEvent]`。
- `_SessionPermissionBroker`（120 s 超时）与 `_SessionAskUserFunc`（300 s 超时）把权限 / AskUser 通过事件 + `threading.Event` 桥接到 UI 线程。
- `core/events.py` 定义 11 种事件：`run.started / run.finished / step.started / llm.started / llm.completed / tool.started / tool.completed / assistant.final / permission.requested / ask_user.requested / turn.completed`（+ `error`）。

### Agent 特性框架 (`core/features/`)
| 顺序 | Feature | 作用 |
|---|---|---|
| 20 | WorktreeFeature | 创建 `WorktreeManager`，支持 git worktree 隔离 |
| 25 | MCPFeature | 注册并重试 MCP 服务器，注入工具 prompt |
| 30 | AgentTeamsFeature | 管理 `TeamManager` 单例，drain 事件 → 注入运行时块 |
| 40 | DelegateModeFeature | Delegate 模式工具白名单 |
| 55 | BudgetFeature | Token 预算解析 / 消耗记账 |
| 60 | PlanModeFeature | 只读 Plan 模式工具白名单 |
| 70 | BackgroundTaskFeature | 后台守护线程子代理 |
| 80 | OutputStyleFeature | 输出风格 prompt 注入 |
| 85 | HookFeature | `.mycode/hooks.json` 生命周期钩子 |
| 90 | VCRFeature | LLM 调用录制 / 回放 |
| 100 | SessionFeature | 会话 ID 与最终快照 |

Plugin Loader（`.mycode/plugins/`）可在不改动主仓的前提下注入额外 hooks / skills / output styles / 自定义 Feature。

### 上下文工程 (`core/context_engine/`)
- `ContextBuilder` 采用 **Late-Binding** ：每轮重新拼装 [L1 系统提示 + MCP prompt + CODE_LAW.md + runtime blocks + 历史]，文件按 mtime 缓存。
- `HistoryManager` 实现 Pi 风格消息树：`_cursor_id` 游标，`fork() / navigate_to() / get_tree() / get_current_branch()` 支持分支、摘要、思考等级、模型切换记录。
- `should_compress()` 当估算 token ≥ `CONTEXT_WINDOW × COMPRESSION_THRESHOLD`（默认 128000 × 0.8 = 102400）且消息数 ≥ 3 时触发；最少保留 `MIN_RETAIN_ROUNDS=10` 轮。
- `summary_compressor.create_summary_generator()` 通过 LLM `compact` profile 总结，120 s 超时回退到硬截断。
- `ObservationTruncator` 按 2000 行 / 50 KB 截断工具输出，超限落盘到 `tool-output/`（7 天保留）。
- `JsonlSessionStore` 写一行一记录的 Pi-Agent 兼容会话文件。
- `TraceLogger` 每会话一份 JSONL + HTML，所有 payload 经 `TraceSanitizer` 脱敏。
- `input_preprocessor` 解析 `@file` mention（最多 5 个），自动追加 `<system-reminder>` 提示模型先读文件。

### 团队协作 (`core/team_engine/`)
- `TeamManager` 单一所有权（仅 `AgentTeamsFeature` 构造，CodeAgent.close() 调用 shutdown），消除 sweep 线程泄漏。
- 文件级持久化：`.teams/<team>/config.json / inbox.jsonl / work_items_*.jsonl / message_status.jsonl / approvals.json`，所有写入有 `.lock` 目录保护。
- `MessageRouter` 五种消息类型（message / broadcast / shutdown_request / shutdown_response / plan_approval_response），状态机 pending → delivered → processed，全部落盘并可重启恢复。
- `ApprovalService` 计划审批：pending → approved/rejected → dispatched（避免重复派发），跨进程持久化。
- `WorkerSupervisor` + `TeammateWorker` 守护线程轮询 inbox，60 s idle 超时；`ExecutionService` + `TurnExecutor` 跑最多 `TEAM_WORKER_MAX_STEPS=8` 步。
- LLM 重试：`TEAM_LLM_MAX_RETRIES=2` + `TEAM_LLM_RETRY_BACKOFF=0.2` + 10% jitter；可重试条件覆盖 429/408/425/409/5xx、RateLimit/Timeout/Connection/APIError 类名匹配。
- 心跳与 sweep：`TEAM_HEARTBEAT_TIMEOUT=300`，每 15–30 s 扫描 stale running，自动 requeue。

### 安全
- `PermissionGate` 软沙箱：项目根内自动放行；越界文件 / 命令向 UI 请求授权；硬黑名单（`.ssh/id_rsa`、`.aws/credentials`、`/etc/shadow`、`C:\Windows\System32` 等）始终拒绝。
- 子代理共享父代理决策缓存（`subagent_gate()`）。
- 工具结果统一 `status / data / text / stats / context / error` 协议，错误码白名单（INVALID_PARAM、NOT_FOUND 等）不计入熔断失败。

### 可观测性
- `TraceLogger` 写 `memory/traces/trace-<session>.jsonl` + 同名 `.html`。
- 事件流（`run_start / step / model_output / parsed_action / tool_call / tool_result / history_compression_* / session_summary`）可在 HTML 中折叠浏览。
- `TraceSpan` 提供 `monotonic` 区间计时与 `threading.local` 父子关系。
- `TraceSanitizer` 自动屏蔽 API key / Bearer token / `/Users/<name>` / `/home/<name>` 段。

---

## 3. 架构总览

```
┌──────────────────────────┐   ┌──────────────────────────────┐
│     TUI                  │   │   Web 桌面端                  │
│  scripts/chat_test_agent │   │   desktop/service (FastAPI)  │
│  prompt_toolkit + Rich   │   │   desktop/web   (Vite + TS)  │
└────────────┬─────────────┘   └────────────┬─────────────────┘
             │ 直连：同线程跑 agent.run()    │ HTTP POST + WebSocket
             │ broker 直接挂 PermissionGate │
             ▼                              ▼
        ┌─────────────────────────────────────────────┐
        │  core/runtime/SessionController             │
        │  ├── AgentSession (守护线程)                 │
        │  ├── queue.Queue[AgentEvent]                │
        │  ├── _SessionPermissionBroker (120s)        │
        │  └── _SessionAskUserFunc      (300s)        │
        └────────────────────┬────────────────────────┘
                             │ event_sink / broker 注入
                             ▼
        ┌─────────────────────────────────────────────┐
        │  agents/codeAgent.py  CodeAgent (ReAct)     │
        │  ├── 11 AgentFeature (worktree → session)   │
        │  ├── ContextBuilder (L1/L2/runtime blocks)  │
        │  ├── HistoryManager (tree, fork, compact)   │
        │  └── _execute_tool → pre/post_tool_use      │
        └──┬──────────────┬──────────────┬────────────┘
           │              │              │
           ▼              ▼              ▼
     ┌──────────┐  ┌────────────┐  ┌──────────────┐
     │ Tools    │  │ LLM        │  │ Context      │
     │ Registry │  │ stream_raw │  │ Engine       │
     │ + MCP    │  │ + retries  │  │ + Trace      │
     └──────────┘  └────────────┘  └──────────────┘
```

TUI 与 Web 共享同一 `AgentEvent` 词表与同一组 session 方法（`send_message / resolve_permission / answer_ask_user / interrupt`），运行时层完全 UI-agnostic。

---

## 4. 快速开始

### 安装

```bash
pip install -r requirements.txt
```

### 首次配置（向导）

```bash
python scripts/first_run_wizard.py
# 内置 7 个 provider preset：DeepSeek / OpenAI / Zhipu / Kimi / Qwen / Ollama / Custom
# 自动写入 .env：LLM_PROVIDER / LLM_MODEL_ID / LLM_API_KEY / LLM_BASE_URL
#               + MODEL_PROFILES / MODEL_POINTER_MAIN 等
```

也可以手动复制 `.env.example` 为 `.env` 修改。

### TUI 启动

```bash
python scripts/chat_test_agent.py

# 指定模型
python scripts/chat_test_agent.py --model gpt-4o --provider openai --api-key sk-xxx

# Plan 模式（只读）
python scripts/chat_test_agent.py --plan

# 继续 / 恢复会话
python scripts/chat_test_agent.py -c
python scripts/chat_test_agent.py -r <session-id>

# 跳过 / 强制首次向导
python scripts/chat_test_agent.py --skip-wizard
python scripts/chat_test_agent.py --wizard
```

### Web 启动

```bash
# 后端 (FastAPI)
uvicorn desktop.service.app:create_app --factory --reload

# 前端 (Vite)
cd desktop/web
npm install
npm run dev
```

浏览器访问 Vite 输出地址即可使用图形界面（会话列表、流式聊天、文件浏览 / 上传、Skill / MCP / Hooks 编辑）。

---

## 5. Slash Commands

TUI 内通过 `SlashCommandRegistry` 注册的命令（共 17 个 distinct，23 条 registration）：

| 命令 | 说明 |
|---|---|
| `/model` | 显示当前模型 + Token 统计 |
| `/model <id>` | 切换模型 |
| `/info` | 显示详细 Token 用量（等价 `/model` 无参） |
| `/plan` | 进入 Plan 只读模式（退出需要 ExitPlanMode 工具） |
| `/sessions` | 列出所有保存的会话 |
| `/resume <id\|index>` | 按 ID / 前缀 / 列表序号恢复会话 |
| `/rename <title>` | 重命名当前会话 |
| `/tree` | 显示当前会话消息树 |
| `/fork <msg-id>` | 在指定消息处分叉新分支 |
| `/thinking [on\|off]` | 显示 / 切换思考等级 |
| `/budget` | 显示剩余 / 总预算 |
| `/budget <amount\|none>` | 设置或清除预算（支持 `500k`、`10万`） |
| `/style [name]` | 显示 / 切换输出风格（default / explanatory / learning） |
| `/save [path]` | 保存会话快照（默认 `memory/sessions/session-manual.json`） |
| `/load <path>` | 加载会话快照 |
| `/team msg <team> <to> <msg>` | 发团队消息 |
| `/team watch <team>` | 查看团队状态 |
| `/delegate [on\|off]` | 启用 / 关闭 Delegate 模式 |
| `/help` | 显示帮助面板 |

非 Slash 命令（直接在主循环处理）：
- `init` —— 通过 `CODE_LAW_GENERATION_PROMPT` 生成 `code_law.md`
- `exit` / `quit` / `q` —— 自动保存并退出

---

## 6. Agent 模式

### Plan 模式
- 只允许 `Read / Grep / Glob / LS / TodoWrite / TaskOutput / EnterPlanMode / ExitPlanMode / AskUser`。
- 退出时通过 `runtime_blocks` 把 `_plan_text` 注入下一轮 system block 一次。

### Delegate 模式
- `agent.set_delegate_mode(True)` 或 `/delegate on`。
- 仅允许团队工具白名单（`team_*` 系列 + `SendMessage` + `TodoWrite` + `AskUser`），由 `DelegateModeFeature.pre_tool_use` 阻断。

### Worktree 模式
- `EnterWorktree` 创建 `.worktrees/<name>` git worktree，调用 `agent._rebind_project_root` 重绑：权限网关、所有工具的 `_project_root/_working_dir/_root`、`ContextBuilder`、`SkillLoader` 全量更新。
- `ExitWorktree` 还原到原始项目根。
- 注意：团队 store（`.teams/.tasks`）仍绑定到 TeamManager 创建时的根（已知 P2）。

### Background 子代理
- `Task` 工具 `run_in_background=true` 通过 `BackgroundTaskRunner` 派发守护线程子代理，结果落盘到 `BG_TASK_OUTPUT_DIR=.tasks/output/`。
- 通过 `TaskOutput` 读取已完成任务的输出。

---

## 7. 项目结构

```
MyCodeAgent/
├── agents/
│   ├── __init__.py
│   └── codeAgent.py                # CodeAgent (ReAct + Feature 编排)
├── core/
│   ├── agent.py                    # Agent 基类
│   ├── background_task.py          # BackgroundTaskRunner
│   ├── budget_tracker.py
│   ├── config.py                   # Pydantic Config (50 字段)
│   ├── constants.py
│   ├── env.py / env_helpers.py
│   ├── events.py                   # AgentEvent / EventType / EventSink
│   ├── exceptions.py
│   ├── hook_system.py              # HookManager
│   ├── llm.py                      # HelloAgentsLLM (含 stream_raw)
│   ├── message.py
│   ├── model_profiles.py
│   ├── output_styles.py
│   ├── plugin_loader.py            # .mycode/plugins/ 加载器
│   ├── response_parser.py
│   ├── session_manager.py          # 磁盘会话目录与索引
│   ├── session_store.py            # 单文件 v2 快照
│   ├── tool_bootstrap.py           # 工具 DI 自动注册
│   ├── vcr.py
│   ├── context_engine/             # ContextBuilder / HistoryManager / Compression / Trace ...
│   ├── features/                   # 11 个内建 AgentFeature
│   ├── runtime/                    # SessionController + AgentSession
│   ├── skills/                     # SkillLoader (两层)
│   ├── team_engine/                # TeamManager + Store / Router / Approval / Worker
│   └── worktree/                   # WorktreeManager
├── tools/
│   ├── base.py / registry.py / permission_gate.py / circuit_breaker.py
│   ├── builtin/                    # 33 个内建工具
│   └── mcp/                        # adapter / client / config / loader / protocol
├── tui/
│   ├── __init__.py
│   ├── streaming.py                # Rich Live 流式输出
│   ├── mention_completer.py
│   ├── permission_dialog.py
│   └── status_line.py
├── desktop/
│   ├── service/                    # FastAPI: app.py / schemas.py
│   └── web/                        # Vite + TS + Tailwind 前端
├── utils/
│   ├── helpers.py / logging.py / serialization.py
│   └── ui_components.py            # EnhancedUI (banner/tree/timer)
├── prompts/
│   ├── agents_prompts/             # L1 / 子代理提示
│   ├── tools_prompts/              # 工具描述（33 个）
│   └── output_styles/              # default / explanatory / learning
├── scripts/
│   ├── __init__.py
│   ├── chat_test_agent.py          # TUI 主入口
│   ├── first_run_wizard.py         # 首次运行向导
│   └── slash_commands.py           # SlashCommandRegistry
├── skills/                         # 内建 Skill 源目录（含 ui-ux-pro-max 等）
├── tests/                          # 79 个 test_*.py + conftest.py + fixtures/ + utils/
├── docs/                           # 设计文档 (含 design/agent_teams/archive/plans/)
├── memory/                         # traces / sessions / todos
├── tool-output/                    # 工具输出溢出落盘 (7 天保留)
├── .mycode/                        # hooks.json / plugins/
├── .mycodeagent/                   # 运行时 Skill 覆盖目录、session workspaces
├── .teams/ .tasks/ .worktrees/     # 运行时数据目录
├── CLAUDE.md
├── README.md
├── LICENSE
├── requirements.txt
├── mcp_servers.json
└── .env / .env.example
```

---

## 8. 子代理类型

`Task` 工具支持三种 dispatch 模式 + 四种 subagent_type：

| 维度 | 取值 | 说明 |
|---|---|---|
| `mode` | `oneshot` | 默认。`SubagentRunner` 跑 `TurnExecutor`，默认 `SUBAGENT_MAX_STEPS=15`，工具 allowlist `{LS, TodoWrite, Glob, Grep, Read}`，可通过 `run_in_background=true` 后台化。 |
| `mode` | `persistent` | 需 `team_name + teammate_name`，调用 `TeamManager.spawn_teammate`（role=developer，denylist=`[Task]`） |
| `mode` | `parallel` | 需 `team_name + tasks[]`，调用 `TeamManager.fanout_work` 一次派发多个 work_item |
| `subagent_type` | `general` | 通用执行 |
| `subagent_type` | `explore` | 偏向调查 / 阅读 |
| `subagent_type` | `summary` | 总结回写 |
| `subagent_type` | `plan` | 仅产出计划，不写文件 |
| `model` | `main` / `light` | `light` 走 `MODEL_POINTER_TASK` 或 `LIGHT_LLM_*` 环境变量 |

---

## 9. 关键环境变量速查

> 完整清单见 `.env.example`。下表覆盖最常用项；`core/config.py` 内共 50+ 字段，均可通过同名环境变量覆盖。

### LLM 基础
| 变量 | 默认值 | 说明 |
|---|---|---|
| `LLM_PROVIDER` | `openai` | 主 provider |
| `LLM_API_KEY` | _(无)_ | 主 API key |
| `LLM_BASE_URL` | _(无)_ | API base URL |
| `LLM_MODEL_ID` | `gpt-3.5-turbo` | 主模型 ID |
| `TEMPERATURE` | `0.7` | 采样温度 |
| `MAX_TOKENS` | _(无)_ | 单次响应 token 上限 |
| `LLM_STREAMING` | `true` | 是否启用 streaming |

每 provider 同时支持专属 key：`OPENAI_API_KEY` / `DEEPSEEK_API_KEY` / `DASHSCOPE_API_KEY` / `MODELSCOPE_API_KEY` / `KIMI_API_KEY` / `MOONSHOT_API_KEY` / `ZHIPU_API_KEY` / `GLM_API_KEY` / `SILICONFLOW_API_KEY` / `OLLAMA_API_KEY` / `VLLM_API_KEY`（以及对应 `*_BASE_URL` / `OLLAMA_HOST` / `VLLM_HOST`）。

### Model Profiles
- `MODEL_PROFILES` —— 逗号分隔的 profile 名列表
- `MODEL_<NAME>_ID / _PROVIDER / _API_KEY / _BASE_URL` —— 每个 profile 一组
- `MODEL_POINTER_MAIN / _TASK / _COMPACT` —— 主对话 / 子任务 / 压缩各自指向的 profile

### Agent / 系统
| 变量 | 默认值 |
|---|---|
| `AGENT_INTERACTIVE` | `true` |
| `MAX_STEPS` | `50` |
| `DEBUG` | `false` |
| `LOG_LEVEL` | `INFO` |
| `SHOW_REACT_STEPS` / `SHOW_PROGRESS` | `true` |

### Context Engine
| 变量 | 默认值 |
|---|---|
| `CONTEXT_WINDOW` | `128000` |
| `COMPRESSION_THRESHOLD` | `0.8` |
| `MIN_RETAIN_ROUNDS` | `10` |
| `SUMMARY_TIMEOUT` | `120` |
| `TOOL_MESSAGE_FORMAT` | `strict` |

### 工具输出
| 变量 | 默认值 |
|---|---|
| `TOOL_OUTPUT_MAX_LINES` | `2000` |
| `TOOL_OUTPUT_MAX_BYTES` | `51200` |
| `TOOL_OUTPUT_TRUNCATE_DIRECTION` | `head` |
| `TOOL_OUTPUT_HEAD_TAIL_LINES` | `40` |
| `TOOL_OUTPUT_DIR` | `tool-output` |
| `TOOL_OUTPUT_RETENTION_DAYS` | `7` |

### 子代理 / Light LLM
| 变量 | 默认值 |
|---|---|
| `SUBAGENT_MAX_STEPS` | `15` |
| `LIGHT_LLM_MODEL_ID / _API_KEY / _BASE_URL` | _(空)_ |
| `LIGHT_LLM_PROVIDER` | `auto` |
| `LIGHT_LLM_TEMPERATURE` | `0.5` |

### Agent Teams
| 变量 | 默认值 |
|---|---|
| `ENABLE_AGENT_TEAMS` (`CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS` 别名) | `false` |
| `AGENT_TEAMS_STORE_DIR` | `.teams` |
| `AGENT_TASKS_STORE_DIR` | `.tasks` |
| `TEAMMATE_MODE` | `auto` (`auto` / `in-process` / `tmux`) |
| `TEAM_DELEGATE_MODE` | `false` |
| `TEAM_WORKER_MAX_STEPS` | `8` |
| `TEAM_LLM_MAX_CONCURRENCY` | `4` |
| `TEAM_LLM_MAX_RETRIES` | `2` |
| `TEAM_LLM_RETRY_BACKOFF` | `0.2` |
| `TEAM_MAX_INBOX_SIZE` | `10000` |
| `TEAM_MAX_WORK_ITEMS` | `5000` |
| `TEAM_HEARTBEAT_TIMEOUT` | `300` |

### Worktree / 风格 / VCR
| 变量 | 默认值 |
|---|---|
| `WORKTREE_STORE_DIR` | `.worktrees` |
| `WORKTREE_BASE_REF` | `fresh` |
| `AGENT_OUTPUT_STYLE` | `default` |
| `VCR_ENABLED` | `false` |
| `VCR_RECORD_MODE` | `new_episodes` |
| `VCR_FIXTURE_DIR` | `tests/fixtures/vcr` |

### Trace / 熔断 / Skill / 后台任务 / MCP
| 变量 | 默认值 |
|---|---|
| `TRACE_ENABLED` | `true` |
| `TRACE_DIR` | `memory/traces` |
| `TRACE_SANITIZE` | `true` |
| `TRACE_HTML_INCLUDE_RAW_RESPONSE` / `TRACE_MD_INCLUDE_RAW_RESPONSE` | `false` |
| `CIRCUIT_FAILURE_THRESHOLD` | `3` |
| `CIRCUIT_RECOVERY_TIMEOUT` | `300` |
| `SKILLS_REFRESH_ON_CALL` | `true` |
| `SKILLS_PROMPT_CHAR_BUDGET` | `12000` |
| `BG_TASK_OUTPUT_DIR` | `.tasks/output` |
| `MCP_CONNECT_MODE` | `manual` (`startup` / `manual` / `disabled`) |

---

## 10. 测试

```bash
# 全量
pytest tests/ -v

# 跳过已知偶发用例
pytest tests/ \
  --ignore=tests/test_agent_teams_parallel.py \
  --ignore=tests/test_team_worker.py \
  -k "not test_grep_success_no_matches and not test_restore_requeues_running_work_items"

# 流式相关
pytest tests/test_llm_streaming.py \
       tests/test_llm_temperature_policy.py \
       tests/test_llm_provider_resolution.py -q
```

- 测试文件：79 个 `test_*.py`
- 测试用例：~936 个（其中 `test_write_tool` 48、`test_todo_write_tool` 47、`test_read_tool` 45、`test_multi_edit_tool` 43、`test_edit_tool` 40）
- 共享 fixture：`tests/conftest.py`（`temp_project / ls_tool / glob_tool / grep_tool`）

---

## 11. 文档索引

- `docs/PROJECT_OVERVIEW.md` —— 四层架构总览（CodeAgent → SessionController → EventSink → UI 适配器）
- `docs/IMPLEMENTATION_SUMMARY.md` —— 实现要点与改造里程碑
- `docs/design/2026-06-26-llm-streaming-design.md` —— LLM Streaming 设计（P1 已入主循环）
- `docs/design/2026-06-26-team-engine-production-hardening.md` —— TeamManager 单一所有权、持久化、Worker 重试硬化
- `docs/agent_teams/` —— AgentTeams 完整功能 / 协议 / 加速验收文档
- `docs/design/` —— 全部历史设计文档（按日期命名）
- `docs/plans/` / `docs/archive/` —— 计划与历史归档
- `CLAUDE.md` —— Agent / 维护者协作指引（命令、风格、commit 规范）

---

## 12. License

MIT License. 详见 [`LICENSE`](./LICENSE)。