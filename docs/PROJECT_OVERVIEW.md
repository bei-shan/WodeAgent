# MyCodeAgent 项目总览（PROJECT_OVERVIEW）

> 本文档是 MyCodeAgent 仓库的"单一文档级深度导览"。读完它，你应当能在脑中复盘整个代码库的运行时分层、关键子系统接口、关键事件流，以及当前已落地与遗留的扩展点。如果某节需要更细的设计细节，请跳转到 `docs/design/`、`docs/agent_teams/`、`docs/plans/` 下对应文档。

---

## 1. 项目定位

MyCodeAgent（内部代号 WodeAgent）是一个面向学习与实验的 AI 编程代理框架。它围绕 **ReAct 循环 + 工具协议 + 上下文工程 + 子代理委派 + 团队协作 + TUI/Web 双前端 + 可观测性** 七条主线展开，使用 Python 3.12+ 实现，兼容 OpenAI / DeepSeek / 通义千问 / 智谱 GLM / Kimi / ModelScope / SiliconFlow / Ollama / vLLM 等多家 LLM 供应商。整套系统坚持"运行时核与 UI 解耦"的工程取向：CodeAgent 只负责跑模型与工具，所有交互均通过事件队列与回调注入完成，因此同一份 agent 内核可以被 prompt_toolkit + Rich 的 TUI 主入口、以及 FastAPI + WebSocket 的桌面/Web 应用同时驱动，且互不感知。

---

## 2. 演进历史（按里程碑）

下表只保留代码与文档可证的关键节点，覆盖从早期 ReAct 原型到 2026-06-26 最新一轮加固。

| 时间 | 里程碑 | 关键产物 |
| --- | --- | --- |
| 早期 | ReAct 主循环 + 32 个内置工具 + 通用工具响应协议 | `agents/codeAgent.py`、`tools/builtin/`、`docs/通用工具响应协议.md` |
| 中期 | 上下文工程引擎（历史树 + L1/L2 系统提示 + 压缩 + Trace） | `core/context_engine/` 八模块、`docs/上下文工程设计文档.md` |
| 中期 | 工具守护：软沙箱权限门 + 熔断器 + 工具输出截断 | `tools/permission_gate.py`、`tools/circuit_breaker.py`、`core/context_engine/observation_truncator.py` |
| 中期 | 多供应商 LLM 客户端 + Model Profiles + Pointer 路由 | `core/llm.py`、`core/model_profiles.py` |
| 中期 | Skill 两层加载（源/runtime）+ 插件系统（`.mycode/plugins/`） | `core/skills/`、`core/plugin_loader.py` |
| 2026-02-17 | AgentTeams MVP：多代理、消息路由、任务板、计划审批 | `core/team_engine/`、`docs/agent_teams/`、`docs/design/2026-02-17-*.md` |
| 2026-06 | **AgentFeature 协议化**：把 11 类能力从 CodeAgent 主类抽离成可插拔特性 | `core/features/`（11 个 feature） |
| 2026-06-26 | **LLM 流式集成**：`HelloAgentsLLM.stream_raw` + Rich Live 实时渲染 | `docs/design/2026-06-26-llm-streaming-design.md`、`tui/streaming.py`、`tests/test_llm_streaming.py` |
| 2026-06-26 | **Team Engine 生产加固**：单点 TeamManager、approval/message 文件级持久化、worker LLM 重试、心跳清扫 | `docs/design/2026-06-26-team-engine-production-hardening.md` |
| 2026-06-26（当前分支 `feat/agent-runtime-decouple`） | **Agent Runtime 解耦**：抽出 `core/runtime/session_controller.py`，TUI/Web 通过同一 `AgentEvent` 队列消费 | `core/runtime/`、`core/events.py`、`desktop/service/app.py` |
| 2026-06-26 | **Web/Desktop 应用**：FastAPI 40 路由 + 1 WebSocket 流，前端 Vite + React + Tailwind | `desktop/service/`、`desktop/web/`、详见 `docs/design/2026-06-26-web-desktop-overview.md`（待补） |

---

## 3. 架构分层

从下到上分七层，每层只依赖比自己更底层的层（features 横切多层是设计允许的例外）。

```
+---------------------------------------------------------------+
|  TUI (scripts/chat_test_agent.py + tui/)   |  Web (desktop/)  |   <- UI 适配层
+---------------------------------------------------------------+
|        SessionController + AgentEvent Queue (core/runtime/)   |   <- 运行时解耦层
+---------------------------------------------------------------+
|  CodeAgent (agents/codeAgent.py)  +  AgentFeature × 11        |   <- Agent 主循环 + 特性
+---------------------------------------------------------------+
|  Context Engine | Team Engine | Worktree | Skills | Plugins   |   <- 子系统层
+---------------------------------------------------------------+
|  ToolRegistry  |  PermissionGate  |  CircuitBreaker  |  MCP   |   <- 工具基础设施
+---------------------------------------------------------------+
|  HelloAgentsLLM (多供应商) + Model Profiles                   |   <- LLM 抽象层
+---------------------------------------------------------------+
|  Config (Pydantic) + .env + mcp_servers.json + .mycode/hooks  |   <- 配置层
+---------------------------------------------------------------+
```

四个最关键的接缝：

1. **CodeAgent ↔ Feature**：通过 `core/features/base.py` 的 `AgentFeature` ABC 协议，11 个特性在构造期被排序、`init()/post_init()`，并在 ReAct 循环的三个钩子（`runtime_blocks` / `pre_tool_use` / `post_tool_use` / `llm_intercept`）介入。
2. **CodeAgent ↔ Runtime**：通过 `core/events.py` 的 `EventSink.emit(AgentEvent)` 单向通道把生命周期事件推给上层；权限请求与 AskUser 通过 `PermissionGate._broker` 与 `AskUser._input_func` 两个注入点反向阻塞等待 UI 决策。
3. **Runtime ↔ UI**：`AgentSession.events` 是一个 `queue.Queue[AgentEvent]`，TUI 在主线程内联消费（实际上 TUI 当前绕过 SessionController，直接同步跑 agent），Web 通过 `run_in_executor` 把同步队列桥接到 WebSocket 异步 I/O。
4. **Tool ↔ Agent**：所有工具实现 `tools/base.Tool` 接口，通过 `tools/registry.ToolRegistry` 暴露成 OpenAI function-calling schema，并被 `core/tool_bootstrap.ToolBootstrap` 通过反射 + 依赖注入自动发现注册。

---

## 4. 关键子系统详解

### 4.1 SessionController & 事件系统（运行时解耦层）

- **设计目标**：让 CodeAgent 内核与任何 UI 完全解耦。一次会话 = 一个守护线程 + 一个事件队列 + 一组阻塞等待器（permission / ask_user）。
- **关键文件**
  - `core/events.py` — 定义 `EventType` 字符串常量（`run.started/run.finished/step.started/llm.started/llm.completed/tool.started/tool.completed/assistant.final/permission.requested/ask_user.requested/turn.completed`）、`AgentEvent` dataclass(`type/payload/step`)、`EventSink` 抽象。
  - `core/runtime/session_controller.py` — `AgentSession`（一会话一线程，名为 `agent-<sid8>`，单 turn 互斥 `_busy_lock`）与 `SessionController`（`create_session/get_session/delete_session/list_sessions`，全局 `threading.Lock` 守 `dict[str, AgentSession]`）。
  - `core/session_manager.py` — 正交的磁盘持久层（`SessionInfo` + `memory/sessions/index.json`，支持 ID、前缀、1-based 索引解析）。
- **关键 API**
  - `SessionController.create_session(agent_factory) -> sid`：生成 12-hex 会话 ID，懒构造 agent，绑定每会话工作目录 `.mycodeagent/sessions/<sid>/`。
  - `AgentSession.send_message(text) -> bool`：忙则返回 False；空闲则把消息塞进事件循环，由 worker 线程驱动 ReAct。
  - `AgentSession.resolve_permission(rid, decision)` / `answer_ask_user(rid, answer)`：UI 用来释放被阻塞的 agent 线程。两端通过 `threading.Event + dict` 配对，permission 120s 超时（默认 denied），AskUser 300s 超时（默认空串）。
  - `AgentSession.close()`：调 `agent.close()` 释放 MCP/Worktree/Trace，并强制解锁所有 pending 等待器，防止线程泄漏。
- **注入点**
  - `agent.event_sink = _QueueEventSink(self.events)` — 把 8 个核心事件推入队列。
  - `agent._permission_gate._broker = _SessionPermissionBroker(self)` — 接管所有出根目录的 sensitive 操作。
  - `ask_tool._input_func = _SessionAskUserFunc(self)` — 接管 AskUser 工具。

### 4.2 AgentFeature 协议（特性可插拔层）

- **设计目标**：把原本散落在 CodeAgent 构造器与主循环里的 MCP 重试、委派门、Hook 执行、Plan 模式过滤、预算记账、Team 事件块、VCR 回放、会话保存等逻辑统一为可组合、可替换的 feature。
- **关键文件**
  - `core/features/base.py` — `AgentFeature` ABC，生命周期：`init(agent) / post_init(agent) / cleanup(agent)`；运行时钩子：`runtime_blocks(agent, step) -> list[str]` / `pre_tool_use(...) -> dict|None` / `post_tool_use(...) -> list[str]` / `llm_intercept(...)`。
  - `core/features/__init__.py` — `BUILTIN_FEATURES` 与 `collect_all_features(agent)`，并接入 `PluginLoader` 发现的插件特性。
- **11 个内置特性（按 order 升序）**

  | order | name | 文件 | 职责 |
  | --- | --- | --- | --- |
  | 20 | worktree | `worktree.py` | 创建 WorktreeManager，支持 EnterWorktree/ExitWorktree 切换 |
  | 25 | mcp_status | `mcp.py` | MCP 服务器懒连接、按步重试、prompt 同步 |
  | 30 | agent_teams | `agent_teams.py` | 唯一持有 TeamManager，runtime_blocks 注入团队事件 |
  | 40 | delegate_mode | `delegate.py` | 委派模式只允许 Team/Todo/AskUser 等 15 个工具 |
  | 55 | budget | `budget.py` | TokenBudget 解析与 `.spend()` 记账 |
  | 60 | plan_mode | `plan_mode.py` | 只读模式工具白名单 + 退出时插入计划文本 |
  | 70 | background_task | `background_task.py` | 后台子代理 daemon-thread 运行器 |
  | 80 | output_style | `output_style.py` | `default/explanatory/learning` 三种风格提示词 |
  | 85 | hook | `hooks.py` | `.mycode/hooks.json` 生命周期钩子（SessionStart/SessionEnd + pre/post tool） |
  | 90 | vcr | `vcr.py` | LLM 录制/回放，包裹 `llm_intercept` |
  | 100 | session | `session.py` | 会话 ID 生成 + 退出快照 |

- **CodeAgent 构造期合奏**：`_init_core` → `collect_all_features(self)` → `for feat: feat.init(self)` → `_init_tools()`（ToolBootstrap 注入 feature 暴露的 `_background_runner`、`_worktree_manager` 等依赖）→ `for feat: feat.post_init(self)`。
- **ReAct 循环合奏**：每步先 `_collect_runtime_blocks(step)` 聚合所有特性的运行时系统块；`_invoke_llm_with_retry` 反向遍历特性构造 `llm_intercept` 链（VCR 在最外层）；`_execute_tool` 串行执行所有 `pre_tool_use`（最先返回 blocked 者短路）与所有 `post_tool_use`（追加系统消息）。

### 4.3 ToolBootstrap & 工具注册

- **关键文件**：`core/tool_bootstrap.py`、`tools/registry.py`、`tools/base.py`。
- **发现机制**：`ToolBootstrap` 用 `pkgutil.iter_modules` 扫描 `tools/builtin/`，`importlib` 导入每个模块，`inspect.getmembers` 找 `Tool` 子类（只接受 `__module__` 与导入模块一致的类，避免 re-export 重复注册）。
- **依赖注入**：`inspect.signature` 读 `__init__` 参数名，从 providers dict 按名取值（典型 provider：`project_root`、`working_dir`、`code_agent`、`team_manager`、`background_runner`、`skill_loader`、`main_llm`、`interactive`）。
- **团队工具特殊处理**：`_TEAM_TOOL_MODULES` 是冻结集（15 个 `team_*` 模块 + `send_message`），自动发现时跳过；当 `enable_agent_teams=true` 时由 `register_team_tools()` 显式注册，注入 `team_manager` 依赖。
- **工具构成（32 个）**
  - 文件操作 6：Read / Write / Edit / MultiEdit / LS / Glob
  - 搜索 1：Grep（优先 ripgrep，回退 Python）
  - 系统 4：Bash（含 INTERACTIVE/DESTRUCTIVE/PRIVILEGE/READ_SEARCH 黑名单）/ TodoWrite / AskUser / Skill
  - 子代理 2：Task / TaskOutput
  - Worktree 2：EnterWorktree / ExitWorktree
  - Plan 2：EnterPlanMode / ExitPlanMode
  - 团队 15：SendMessage + 14 个 `team_*`
- **模型切换不暴露为工具**：模型切换是用户策略，不在 LLM 工具表里。用户通过 `/model <id>` 触发 `CodeAgent.switch_model()`；框架按角色（MAIN / TASK / COMPACT pointer）在 `core/model_profiles.py` 内部路由。
- **响应协议**：`ToolRegistry` 把所有返回值规范化为 `{status, data, text, stats, context, error}`，并在 Write/Edit/MultiEdit 上自动注入 `expected_mtime_ms` 与 `expected_size_bytes`（来自 Read 缓存）实现乐观锁。
- **守护机制**：`PermissionGate`（根内自动放行，根外咨询用户或拒绝；硬编码 `_ALWAYS_DENY_PATTERNS` 永远拒绝系统目录/SSH 私钥/AWS 凭证/.env.production）+ `CircuitBreaker`（每工具 CLOSED/OPEN/HALF_OPEN，失败 3 次熔断 5 分钟，参数错误等 6 类错误码不计入失败）。
- **MCP**：`tools/mcp/` 六模块（config/client/loader/adapter/protocol/__init__）支持 stdio + streamable-HTTP，三种连接模式 `startup/manual/disabled`，manual 模式后台线程懒连接，pending server 在首次 ReAct 步重试。

### 4.4 Context Engine（上下文工程引擎）

- **关键文件（八模块）**
  - `history_manager.py` — Pi-Agent 风格的消息树。每个 Message 带 `parent_id`，`_cursor_id` 指向当前分支末梢；支持 `fork(target_id)`（回溯生成新分支）、`navigate_to(target_id, summarize=True)`（可选触发 LLM 给废弃分支生成摘要）、`get_tree()`、`get_current_branch()`。条目类型：ENTRY_MESSAGE / ENTRY_COMPACTION / ENTRY_BRANCH_SUMMARY / ENTRY_MODEL_CHANGE / ENTRY_THINKING_CHANGE / ENTRY_LEAF / ENTRY_LABEL / ENTRY_SESSION_INFO。
  - `context_builder.py` — Late Binding 组装。每步 `build_messages()` 重新拼 `[L1 base + tools usage_notes + disabled-tools, MCP tools prompt, CODE_LAW.md, runtime blocks, history]`。L1/CODE_LAW 都按 mtime 缓存，tools usage_notes 来自 ToolRegistry 实时输出。
  - `observation_truncator.py` — 统一工具输出截断。阈值 `MAX_LINES=2000` / `MAX_BYTES=51200`，方向 `head|tail|head_tail`，溢出时整段写入 `tool-output/tool_<ts>_<tool>.json`（7 天保留，10% 概率触发清理），返回截断元数据 + 完整文件路径。
  - `summary_compressor.py` — 压缩生成器。优先用 `compact` 模型 profile，prompt 模板 SUMMARY_PROMPT（A5），单 worker `ThreadPoolExecutor` + 120s 超时；超时返回 None 让 HistoryManager 退化为硬截断。
  - `trace_logger.py` — 每会话写一份 JSONL + 配套 HTML。事件覆盖 run_start/run_end / system_messages / step / message_written / model_output / parsed_action / tool_call / tool_result / error / finish / session_summary 与所有 `history_compression_*`。HTML 可选含原始响应（受 `TRACE_HTML_INCLUDE_RAW_RESPONSE` 控制）。`TraceSpan` 支持嵌套 span 与 `event(name, **attrs)`。
  - `trace_sanitizer.py` — 入盘前脱敏。键名命中 `api_key/secret/token/password/authorization` 等替换为 `***`，正则脱敏 `sk-…` 与 `Bearer …`，路径中的 `/Users/<name>` 与 `/home/<name>` 也会被遮蔽。
  - `input_preprocessor.py` — 解析 `@file` mention（正则 `(?<![a-zA-Z0-9])@([a-zA-Z0-9/._-]+)`），去重后限 5 个，追加 `<system-reminder>` 指示模型先 Read 再回答（设计决策 E4：仅英文路径）。
  - `jsonl_store.py` — Pi-Agent 兼容的 JSONL 会话树持久层。session 头 + message/leaf/label 条目，`set_leaf` 等价于 fork 在磁盘上的体现，`get_path_to_root(leaf_id)` 走 parentId 链。
- **压缩触发**：`should_compress(pending)` 在估算 token ≥ `context_window * threshold` 且消息数 ≥ 3 时返回 True。默认 `128000 * 0.8 = 102400`。回合数 ≤ `min_retain_rounds`（默认 10）时拒绝压缩。压缩过的旧分支以 ENTRY_COMPACTION 形式保留，不真正删除。

### 4.5 Team Engine（多代理协作引擎）

- **关键文件**：`core/team_engine/` 共 18 个模块，核心是 `manager.py / store.py / task_board_store.py / message_router.py / approval.py / supervisor.py / worker.py / execution.py / turn_executor.py`。
- **TeamManager**：单点持有者是 `AgentTeamsFeature`。构造时初始化 TeamStore + TaskBoardStore + MessageRouter + ApprovalService + WorkerSupervisor + ExecutionService + LLM 信号量（`TEAM_LLM_MAX_CONCURRENCY=4`）+ 后台 `TeamSweep` 心跳线程。暴露 `create_team / delete_team / cleanup_team / spawn_teammate / send_message / mark_message_processed / get_status / create_board_task / claim_next_board_task / list_plan_approvals / respond_plan_approval / drain_events / fanout_work / collect_work / retry_failed_work / has_worker / export_state / import_state / shutdown`。
- **TeammateWorker**：每个队友一条线程，约 20ms 轮询自己的 `poll_fn`（实际是 `TeamManager._process_member_inbox`），idle 60s 后停止，状态机 `starting/active/idle/stopping/stopped`，停止时触发 `EVENT_WORKER_STOPPED`。
- **存储布局**
  - `.teams/<team>/`：`config.json`、每成员 `inbox_*.jsonl` + `.lock`、按 owner 拆分的 `work_items_<owner>.jsonl`、`message_status.jsonl`、`approvals.json`，均为 mkdir 原子锁 + 超时 stale 回收。
  - `.tasks/<team>/`：每任务 `task_<id>.json` + `_meta.json`（自增 ID），守锁 `.board.lock`。
- **任务板**：状态 `pending/in_progress/completed/canceled`，双向依赖 `blocked_by + blocks`，`claim_next_task()` 原子地把 `pending + owner='' + blocked_by=[]` 翻成 `in_progress`，完成时自动从依赖方的 blocked_by 清除自己。
- **消息协议**：5 种类型（`message/broadcast/shutdown_request/shutdown_response/plan_approval_response`），3 状态（`pending/delivered/processed`），状态写 `message_status.jsonl`，TeamManager 启动时从文件还原（解决"重建 manager 丢状态"问题）。
- **审批服务**：状态机 pending → approved/rejected → dispatched。每次状态变更都落 `approvals.json`，`claim_next_approved_request` 必须先把 dispatched=True 持久化再创建 work_item，避免重复派发。`import_state` 会从旧 session 快照回填到文件。
- **Worker LLM 重试加固**（2026-06-26）：`TEAM_LLM_MAX_RETRIES=2`、`TEAM_LLM_RETRY_BACKOFF=0.2`，可重试判定 = 异常类名含 RateLimit/Timeout/Connection/APIError ∨ status_code∈{408,409,425,429,5xx} ∨ 消息含 rate limit / 429 / timeout / connection / temporarily / 503；±10% 抖动；backoff sleep 在 LLM 信号量外释放；空 final response 视为失败。
- **心跳清扫**：worker 跑 work_item 时 `update_work_item_heartbeat`；`TeamSweep` 每 `min(30, heartbeat_timeout/2)` 秒扫一次，超 `TEAM_HEARTBEAT_TIMEOUT=300` 的 running item 自动 requeue。`shutdown()` 5s join。
- **TeamRetry 状态守卫**：只接受 failed/canceled，其他状态返回 CONFLICT；未知 work_id 返回 NOT_FOUND。

### 4.6 Skill / Plugin / Hook 三联体

- **Skill 两层**
  - 源目录 `skills/`（git 跟踪）+ runtime 目录 `.mycodeagent/skills/`（写入，首启 seed）。
  - SKILL.md 为 YAML frontmatter + 正文，name 必须 `^[a-z0-9]+(?:-[a-z0-9]+)*$`。
  - 加载顺序：runtime 先扫，源目录后扫，runtime 覆盖同名。
  - 调用：`Skill` 工具支持 `load/list/create`；环境变量 `SKILLS_REFRESH_ON_CALL=true` 在每次调用刷新，`SKILLS_PROMPT_CHAR_BUDGET=12000` 限制注入 prompt 的字符预算。
- **Plugin（`core/plugin_loader.py`）**
  - 目录：`.mycode/plugins/<name>/plugin.json`，跳过以 `.` 或 `_` 起始的子目录，按字母序加载。
  - 清单字段：`name/version/description/features{hooks, skills, output_styles, custom_features}`。
  - 三类内置 feature 包装类：`_PluginHookFeature(order=86)` 合并到 `.mycode/hooks.json`、`_PluginSkillFeature(order=15)` 触发 `_refresh_skills_prompt`、`_PluginOutputStyleFeature(order=81)` 重载 output style manager。
  - 自定义 Python：`importlib` 加载 `.py` 文件，把目录加 `sys.path`，找第一个 `AgentFeature` 子类实例化。
- **Hook（`.mycode/hooks.json`）**：由 `HookFeature` 加载并交给 `HookManager` 执行。生命周期事件：SessionStart（构造期）/ SessionEnd（cleanup）/ PreToolUse / PostToolUse。Pre 可阻断工具调用并附加系统消息。

### 4.7 Web / Desktop 应用

> 完整接口表、前端组件树、WebSocket 协议、鉴权讨论详见后续将补的 `docs/design/2026-06-26-web-desktop-overview.md`。

- **后端**：`desktop/service/app.py`，FastAPI，**40 个 REST 路由 + 1 个 WebSocket 流**，分为 11 个资源组：Health / Sessions / Messages-Control / Permissions-AskUser / Session Config / Agent Teams / Files / Info(Models/Tools/MCP) / Skills / MCP Servers / Hooks。
- **会话生命周期**：`POST /api/sessions` 创建 → `POST /messages` 触发一轮 → `WS /stream` 推事件 → `POST /permissions/{rid}/resolve` 或 `POST /ask-user/{rid}/answer` 解阻塞 → `POST /interrupt` 中断 → `DELETE` 清理（含磁盘）。
- **WebSocket `ws://host/api/sessions/{sid}/stream`**
  - 单向 Server → Client；服务端调 `loop.run_in_executor(None, session.events.get, True, 1.0)` 把同步队列桥接到异步 I/O，每秒检查一次断连。
  - 仅转发 `_STREAM_EVENTS` 白名单内的 12 种事件（8 个核心 + 4 个会话层 permission/ask_user/turn_done/error）。
  - 帧形如 `{"type": str, "payload": dict, "step": int}`，payload 先经 `_sanitize_payload`：剥离 `_` 开头键、截断 >50KB 字符串。
  - 断线后服务端不删除会话，便于客户端重连。
- **前端**：`desktop/web/`，Vite + React + TypeScript + Tailwind，构建产物在 `dist/`。
- **启动/关闭**：`startup` 仅日志；`shutdown` 遍历 `controller.list_sessions()` 调 `delete_session`，确保所有 worker 线程与 MCP 客户端释放。
- **安全注意**：当前 CORS 是 `allow_origins=["*"]`，WebSocket 无 Origin/Token 校验，仅适合本地桌面用途。

---

## 5. 数据流时序示例

下面是用户在 Web 端输入一句话直到 TUI/前端看到 token 流出来的完整调用链。TUI 路径几乎相同，差别只在最后一步事件消费方式（TUI 在主线程同步消费 → Rich Live；Web 通过 `run_in_executor` → WebSocket → 浏览器）。

```
浏览器 ──fetch──► POST /api/sessions/{sid}/messages
                                  │
                                  ▼
                AgentSession.send_message(text)              (主线程返回，立刻可监听 WS)
                                  │
                                  ▼
                  agent-<sid8> 守护线程
                                  │
                                  ▼
                CodeAgent.run(text):
                  emit RUN_STARTED ──────────────► AgentEvent → events Queue
                  loop step in [1..MAX_STEPS]:
                    emit STEP_STARTED ───────────► Queue
                    _collect_runtime_blocks(step) ← 11 features.runtime_blocks(...)
                    ContextBuilder.build_messages() ← L1 + tools + CODE_LAW + blocks + history
                    _invoke_llm_with_retry():
                      emit LLM_STARTED ──────────► Queue
                      llm.stream_raw(...)
                        ► 每 delta 触发 callback → TUI Rich Live / Web (尚未流式)
                      emit LLM_COMPLETED ────────► Queue
                    parse_action(response):
                      if tool_call:
                        for feat: pre_tool_use → 可能 BLOCKED
                        permission_gate.ask() ─── 出根目录? → broker
                                                          │
                                                          ▼
                                                    emit PERMISSION_REQUESTED ► Queue
                                                    AgentSession 阻塞在 threading.Event
                                                          │
                                                    浏览器决策 POST /permissions/{rid}/resolve
                                                          │
                                                    session.resolve_permission(rid, ...)
                                                          │  (释放 Event)
                                                          ▼
                        emit TOOL_STARTED ─────────► Queue
                        tool.execute(...)
                        observation_truncator.truncate(...)
                        for feat: post_tool_use
                        emit TOOL_COMPLETED ───────► Queue
                      else if assistant_final:
                        emit ASSISTANT_FINAL ──────► Queue
                        break
                  emit RUN_FINISHED ───────────────► Queue

                  emit TURN_COMPLETED ─────────────► Queue

       浏览器 WS 循环：run_in_executor(events.get, 1s)
                  → JSON 帧 ws.send_json({type, payload, step})
                  → React Store 更新 → 渲染消息卡片 / 工具树 / 权限弹窗
```

---

## 6. 测试策略

- **规模**：`tests/` 下共 **79 个 `test_*.py`**，约 **936 个 `def test_`**。`conftest.py` 提供 `temp_project / ls_tool / glob_tool / grep_tool` 共享 fixture（背后是 `tests/utils/test_helpers.py::create_temp_project`）。
- **覆盖类别**
  - 工具单元：Read(45)/Write(48)/Edit(40)/MultiEdit(43)/TodoWrite(47)/Glob/Grep/Bash 等单文件高密度测试。
  - LLM 抽象：`test_llm_streaming.py`（stream_raw 请求参数、delta 回调、tool_call 合并、usage 合并、stream_options 回退）、`test_llm_temperature_policy.py`、`test_llm_provider_resolution.py`。
  - Team Engine：`test_team_worker_retry.py`（429 重试 vs 不可重试快速失败、信号量在 backoff 前释放）、`test_team_approval_persistence.py`（pending/approved/dispatched 三态持久化与回填）、`test_message_status_survives_manager_recreate`、`test_team_worker.py`、`test_agent_teams_parallel.py`。
  - 上下文工程：history_manager / context_builder / observation_truncator / trace_logger / trace_sanitizer / input_preprocessor / jsonl_store 各自独立用例。
  - 协议：`test_protocol_compliance.py`（通用工具响应协议合规性，作为 `run_all_tests.py` 入口）。
  - VCR：`tests/fixtures/vcr/` 下存放录制 fixture；`VCRFeature` 在 `llm_intercept` 链最外层。
- **运行**

  ```bash
  pytest tests/ -v
  # 跳过两个偶发 flaky 文件 + 两个名字筛选项
  pytest tests/ --ignore=tests/test_agent_teams_parallel.py \
                --ignore=tests/test_team_worker.py \
                -k "not test_grep_success_no_matches and not test_restore_requeues_running_work_items"
  ```

---

## 7. 配置与扩展点

### 7.1 Config（`core/config.py`，Pydantic BaseModel）

50 个字段，16 个分组：LLM / System / Agent / AgentTeams / Context / Tool-Output / Subagent / Worktree / VCR / Output / Trace / MCP / Circuit-Breaker / Skills / Background-Task / Team-Advanced。`.env` 通过 `find_dotenv + load_dotenv(override=False)` 加载。LLM 凭证（`LLM_API_KEY` / `LLM_BASE_URL`）不在 Config 字段里，而是直接被 `core/llm.py` 按 provider 读取，同时支持 `OPENAI_API_KEY / DEEPSEEK_API_KEY / KIMI_API_KEY / MOONSHOT_API_KEY / ZHIPU_API_KEY / GLM_API_KEY / DASHSCOPE_API_KEY / MODELSCOPE_API_KEY / SILICONFLOW_API_KEY / OLLAMA_HOST / OLLAMA_API_KEY / VLLM_HOST / VLLM_API_KEY / SILICONFLOW_BASE_URL` 等显式 provider 变量。

关键默认值速查（与 README 中存在出入的项**已修正**）：

| 变量 | 默认 | 备注 |
| --- | --- | --- |
| `CONTEXT_WINDOW` | **128000** | （README 旧值 200000 有误） |
| `COMPRESSION_THRESHOLD` | 0.8 | |
| `MIN_RETAIN_ROUNDS` | 10 | |
| `SUMMARY_TIMEOUT` | 120 | |
| `MAX_STEPS` | 50 | 主 agent |
| `SUBAGENT_MAX_STEPS` | 15 | Task 子代理 |
| `TEAM_WORKER_MAX_STEPS` | 8 | TeamWorker 每 work_item |
| `TEAM_LLM_MAX_CONCURRENCY` | 4 | |
| `TEAM_LLM_MAX_RETRIES` / `TEAM_LLM_RETRY_BACKOFF` | 2 / 0.2 | 2026-06-26 加固 |
| `TEAM_HEARTBEAT_TIMEOUT` | 300 | |
| `TOOL_OUTPUT_MAX_LINES` / `MAX_BYTES` / `RETENTION_DAYS` | 2000 / 51200 / 7 | |
| `CIRCUIT_FAILURE_THRESHOLD` / `RECOVERY_TIMEOUT` | 3 / 300 | |
| `MCP_CONNECT_MODE` | manual | 可选 startup/manual/disabled |
| `WORKTREE_STORE_DIR` / `WORKTREE_BASE_REF` | .worktrees / fresh | |
| `TRACE_ENABLED` / `TRACE_DIR` / `TRACE_SANITIZE` | true / memory/traces / true | |
| `VCR_ENABLED` / `VCR_RECORD_MODE` / `VCR_FIXTURE_DIR` | false / new_episodes / tests/fixtures/vcr | |
| `ENABLE_AGENT_TEAMS` | false | 也接受 `CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS` |
| `LLM_STREAMING` | true | 2026-06-26 上线 |

> 注：README 早期列过 `PERMISSION_SOFT_SANDBOX`，但当前代码并无该变量；软沙箱开关由 `tools/permission_gate.py` 自己读，应以代码为准。

### 7.2 扩展点速查

| 扩展位置 | 文件/路径 | 作用 |
| --- | --- | --- |
| LLM Provider | `core/llm.py` | 在 `_resolve_*` 中新增一个分支即可接入新供应商 |
| Model Profile / Pointer | `.env` 的 `MODEL_PROFILES` + `MODEL_<NAME>_*` + `MODEL_POINTER_*` | 同时声明多个 profile，按 pointer 路由 main / task / compact 三类调用 |
| MCP Server | `mcp_servers.json` | `{mcpServers: {name: {command, args}}}`；可在 Web 端 `/api/mcp/servers` CRUD |
| 内置工具 | 在 `tools/builtin/` 新建文件，继承 `Tool` | `ToolBootstrap` 自动发现注册 |
| AgentFeature | 在 `core/features/` 新建文件，继承 `AgentFeature` | 加入 `BUILTIN_FEATURES` 或通过插件加载 |
| Skill | `skills/<name>/SKILL.md` 或 `.mycodeagent/skills/<name>/SKILL.md` | YAML frontmatter + Markdown 正文，支持 `$ARGUMENTS` |
| Plugin | `.mycode/plugins/<name>/plugin.json` | hooks / skills / output_styles / custom_features 四类 |
| Hook | `.mycode/hooks.json` 或 Web `/api/hooks` PUT | SessionStart/End + PreToolUse/PostToolUse |
| Output Style | `prompts/output_styles/<name>` | `default / explanatory / learning`，可通过 `AGENT_OUTPUT_STYLE` 或 `/style` 命令切换 |

### 7.3 入口与脚本

- TUI 主入口：`python scripts/chat_test_agent.py`，参数含 `--model / --provider / --api-key / --base-url / --temperature / --teammate-mode / --plan / -c / -r / --wizard / --skip-wizard`。
- 首启向导：`python scripts/first_run_wizard.py`，预设 7 个 provider（DeepSeek / OpenAI / Zhipu / Kimi / Qwen / Ollama / Custom），写入 `.env` 完整模板。
- Slash 命令注册器：`scripts/slash_commands.py`，`SlashCommandRegistry.dispatch` 在主循环里接管 `/` 开头输入。当前实现 17 个 distinct 命令（23 条注册），含 README 未列出的 `/sessions / /resume / /rename / /tree / /fork / /thinking`，以及 `/budget <amount|none>` 设置/清除两种模式。
- Web/Desktop 启动：`uvicorn desktop.service.app:create_app --factory --reload`（前端在 `desktop/web/` 中 `npm run dev`）。

---

## 8. 已知限制与后续方向

按代码与最近一轮加固文档可证的待办，分四档：

**P0 / P1（基本机制已上线，仍有遗留打磨）**

- LLM 流式：已集成主循环，但 reasoning delta 暂未渲染成 thinking block；tool_calls 仍等待整段 assistant message；VCR 仅录原始响应级，没有 token 事件；TurnExecutor（Team Engine 内）还没切流式；缺少 Anthropic-native streaming 适配。
- Team Engine：`.teams/.tasks` 仍绑定在 TeamManager 构造期的 project_root，进入 worktree 后 worker 文件操作虽已重绑路径但状态目录不变（设计文档标记的 P2 项）。

**P2（结构拆分与可观测性）**

- TeamManager 责任拆分：建议拆为 `WorkerLoopService / TeamStateSnapshotService / TeamDisplayService`。
- 重试可观测：每次重试的 attempt 元数据、approval 结构化事件、message-status 文件损坏告警。
- Web 端缺少 Origin/Token 校验与 CORS 收敛，当前仅适合本地桌面场景。

**P3（功能性扩展）**

- TUI 仍绕过 SessionController 同步驱动 agent；若希望 TUI 也能多会话并行，需要把 TUI 改造为 SessionController 的另一个消费者。
- 上下文压缩对 tool_call 链的语义保留依然偏弱，长链复杂多步任务后回看时容易丢前提。
- 桌面端目前只有读写文件、查看模型/工具/MCP 状态、管理 Skills/Hooks/MCP Server、监听事件流，没有内置的 Trace 浏览器或 Team 可视化。

**P4（生态/对外）**

- 模型 Profile 与 Pointer 体系成熟，但 `LIGHT_LLM_*`、`compact` profile 与 main profile 的关系仍以约定为主，缺少官方对应表。
- 公开发布需补 LICENSE 说明、第三方依赖审计、最小可复现 Docker 化与 Windows/Linux/macOS 三平台 CI。

---

附：与本文档配套的延伸阅读

- `docs/上下文工程设计文档.md` — Context Engine 的原始设计文稿与思考过程。
- `docs/通用工具响应协议.md` — `{status, data, text, stats, context, error}` 协议规范。
- `docs/agent_teams/AgentTeams功能设计文档.md` — Team Engine 的功能与配置全集。
- `docs/design/2026-06-26-llm-streaming-design.md` — LLM 流式集成与测试矩阵。
- `docs/design/2026-06-26-team-engine-production-hardening.md` — Team Engine 一轮加固验证报告。
- `docs/IMPLEMENTATION_SUMMARY.md` — 各模块实现摘要（持续滚动更新）。
