# Web / Desktop 应用总体设计

- 状态：v1 已落地（FastAPI 服务 + Vite/React 前端）
- 日期：2026-06-26
- 关联代码：`desktop/service/app.py`、`desktop/web/src/`、`core/runtime/session_controller.py`、`core/events.py`
- 关联设计：`docs/design/2026-06-26-llm-streaming-design.md`（流式协议依赖）

## 1. 背景与目标

MyCodeAgent 最早只有一个 TUI 入口（`scripts/chat_test_agent.py`），所有 UI 交互（权限询问、`AskUser`、流式输出、`/slash` 命令）都直接耦合在 `prompt_toolkit` 主线程上。这种形态在单人本地调试时足够用，但在以下场景下暴露出问题：

1. **多会话并发**：TUI 是单进程单线程，无法在同一台机器上同时跑多个 agent 会话。
2. **跨设备访问**：希望在桌面、平板或浏览器里访问本地 agent，而不是只能在终端里操作。
3. **可视化需求**：工具调用树、文件浏览、Hook/MCP/Skill 等"管理面"需要图形化界面才能高效查看与编辑。
4. **远端/无人值守**：把 agent 作为本地服务跑起来，由前端、IDE 插件、桌面壳（Tauri/Electron）等多个消费者复用。

为此项目沿 `feat/agent-runtime-decouple` 分支把 agent 核心从 UI 中剥离，新增了两层：

- `desktop/service/`：FastAPI HTTP + WebSocket 服务，运行在本机。
- `desktop/web/`：Vite + React + Tailwind 构建的浏览器单页应用，调用上述服务。

目标是让 TUI 和 Web 端共用同一套 agent 核心，UI 仅是不同形态的"消费者"。

## 2. 总体架构

```
+--------------------+    +--------------------+
|   TUI (终端)        |    |  Web / Desktop UI  |
| scripts/chat_test   |    |  desktop/web (Vite) |
+---------+----------+    +---------+----------+
          |                          |
          |  同线程直连                | HTTP REST + WebSocket
          |                          |
          v                          v
   +------------------------------------------+
   |   SessionController  (core/runtime/)     |
   |   - dict[sid, AgentSession]              |
   |   - 每个会话一个 daemon Thread             |
   |   - queue.Queue[AgentEvent] 事件总线       |
   |   - PermissionBroker / AskUserBroker      |
   +------------------+-----------------------+
                      |
                      v
   +------------------------------------------+
   |   CodeAgent (agents/codeAgent.py)        |
   |   - ReAct 循环                            |
   |   - features/ 11 个能力插件                |
   |   - tools/builtin/ 32 个工具               |
   |   - context_engine/ 历史与压缩             |
   |   - team_engine/ 多智能体协作              |
   +------------------------------------------+
```

四层契约：

1. **CodeAgent**：纯 Python，运行在工作线程上，按 ReAct 循环驱动 LLM 和工具，把生命周期信号通过 `EventSink.emit(AgentEvent)` 推出。
2. **SessionController + AgentSession**（`core/runtime/session_controller.py`）：UI 无关。每个会话独占一个 `daemon Thread`、一个 `queue.Queue[AgentEvent]`、一份 `workspace_dir`，并通过 `_SessionPermissionBroker`（120s 超时）和 `_SessionAskUserFunc`（300s 超时）把权限询问/用户提问以事件形式投递到队列、阻塞工作线程直到 UI 回填。
3. **事件总线**：`core/events.py` 定义的 `EventType` 共 8 个核心事件 + `core/runtime` 中 4 个会话层事件，合计 12 个事件类型，TUI 和 Web 都从同一个队列消费同一份事件。
4. **UI 适配层**：TUI 直接 `agent.run()` 同步驱动，把 `permission_dialog.ask` 和 `prompt_toolkit` 输入接到 broker 上；Web 后端把同步队列通过 `loop.run_in_executor(...)` 桥接到 WebSocket，前端再通过 React 状态机渲染。

`SessionController` 不依赖 FastAPI，纯标准库（`queue`、`threading`、`uuid`），这是 UI 解耦的根本保证。

## 3. 后端 API 总览（`desktop/service/`）

后端入口是 `desktop/service/app.py::create_app(agent_factory)`。它把一个 `SessionController` 实例放进 `app.state.controller`，所有 HTTP/WS 处理函数从这里取会话。

工作目录：`MYCODEAGENT_DATA_DIR/sessions/<sid>/`，每会话独立。会话持久化由 `core/session_manager.py` 写到 `memory/sessions/`，重启后可重新激活。

CORS 当前是 `allow_origins=["*"]`，**未启用任何鉴权**——默认仅供本机访问，远程暴露需要前置 reverse proxy 自带 auth。

### 3.1 REST 端点（40 个）

| 分组 | 方法 | 路径 | 说明 |
|---|---|---|---|
| 健康 | GET | `/api/health` | 存活检查 + 活跃会话数 |
| 会话 | POST | `/api/sessions` | 新建会话（201） |
| | GET | `/api/sessions` | 列出会话（含内存活跃 + 磁盘持久化），带 `busy`/`pinned` 标志 |
| | GET | `/api/sessions/{sid}` | 会话详情 |
| | POST | `/api/sessions/{sid}/activate` | 把磁盘上的快照重新加载到内存 |
| | DELETE | `/api/sessions/{sid}` | 删除会话（含磁盘快照 + workspace） |
| | PUT | `/api/sessions/{sid}/rename` | 重命名会话 |
| | POST | `/api/sessions/{sid}/pin` | 切换置顶标志 |
| | GET | `/api/sessions/{sid}/history` | 读取持久化消息（每条上限 5000 字符） |
| 消息 / 控制 | POST | `/api/sessions/{sid}/messages` | 发送用户消息，触发一轮 ReAct（busy 时 409） |
| | POST | `/api/sessions/{sid}/interrupt` | 尽力中断当前回合 |
| | POST | `/api/sessions/{sid}/upload` | multipart 文件上传到 workspace |
| 权限 / AskUser | POST | `/api/sessions/{sid}/permissions/{rid}/resolve` | `{decision: granted\|denied}` |
| | POST | `/api/sessions/{sid}/ask-user/{rid}/answer` | `{answer}` |
| 会话配置 | GET / PUT | `/api/sessions/{sid}/config` | 模型、`enable_agent_teams`、`plan_mode` 等开关 |
| 团队 | GET | `/api/sessions/{sid}/teams` | 列出团队及 task board 计数 |
| | POST | `/api/sessions/{sid}/teams` | 新建团队（agent-teams 未启用时 400） |
| 文件 | GET | `/api/sessions/{sid}/files` | 列目录（限定在 workspace 内） |
| | GET | `/api/sessions/{sid}/files/content` | 读取文件（带行号） |
| | GET | `/api/sessions/{sid}/files/download` | 下载文件为 `application/octet-stream` |
| 信息 | GET | `/api/models` | 已配置的 LLM profiles |
| | GET | `/api/tools` | 注册的工具列表 |
| | GET | `/api/mcp/status` | MCP 服务器状态 |
| Skills | GET / POST | `/api/skills` | 列出 / 新建（写入 `.mycodeagent/skills/`） |
| | GET | `/api/skills/{name}/content` | SKILL.md 内容 + frontmatter |
| | PUT / DELETE | `/api/skills/{name}` | 修改 / 删除 |
| | POST | `/api/skills/validate` | 仅校验，不写盘 |
| | GET / PUT | `/api/skills/{name}/enabled` | 启用/禁用（写 `skill_state.json`） |
| MCP servers | GET / POST | `/api/mcp/servers` | 读 / 写 `mcp_servers.json` |
| | PUT / DELETE | `/api/mcp/servers/{name}` | 修改 / 删除 |
| | GET / PUT | `/api/mcp/servers/{name}/enabled` | 启用/禁用 |
| Hooks | GET / PUT | `/api/hooks` | 读 / 写 `.mycode/hooks.json` |

文件类端点会把请求路径 `.relative_to(root)` 检查，防止越权访问 workspace 之外的文件。

### 3.2 WebSocket 事件流

- URL：`/api/sessions/{sid}/stream`
- 方向：**server → client**，handler 从不读 `ws.receive()`，客户端帧被忽略。
- 桥接：`loop.run_in_executor(None, session.events.get, True, 1.0)` 把同步 `queue.Queue` 包成 async，每秒检查一次断开。
- 帧格式：`{"type": str, "payload": dict, "step": int}`。
- 过滤白名单：`_STREAM_EVENTS` 包含 12 个类型——
  - 核心 8：`run.started` / `run.finished` / `step.started` / `llm.started` / `llm.completed` / `tool.started` / `tool.completed` / `assistant.final`
  - 会话 4：`permission.requested` / `ask_user.requested` / `turn.completed` / `error`
- payload 清洗（`_sanitize_payload`）：去除 `_` 前缀字段；字符串 > 50 000 字符截断；非 JSON 原生类型用 `str()` 兜底。
- 断线策略：`WebSocketDisconnect` 与其他异常都被吞下，**会话本身不被清理**，客户端可以重连继续消费后续事件。

权限 / AskUser 闭环示例：

```
worker thread          UI (浏览器)
     |                      |
     | emit permission.requested → WebSocket
     |                      |
     | wait threading.Event |
     |                      | POST /permissions/{rid}/resolve
     | event.set() ← session.resolve_permission(rid, "granted")
     | 继续执行工具          |
```

启动/关闭钩子：`startup` 写日志；`shutdown` 遍历 `controller.list_sessions()` 调 `delete_session`，让所有 worker 线程优雅结束、释放 MCP 客户端。

## 4. 前端结构（`desktop/web/`）

技术栈：Vite 5 + React 18 + TypeScript 5.5 + Tailwind 3.4，纯前端 SPA，无路由库，靠组件内部状态切换面板。生产构建落在 `desktop/web/dist/`，开发用 `npm run dev`。

源文件极少，刻意保持薄：

```
desktop/web/src/
├── main.tsx        # ReactDOM 入口
├── App.tsx         # 单文件包含所有组件 + 状态机
├── api.ts          # 类型定义 + fetch 封装 + connectStream(WebSocket)
└── index.css       # Tailwind 入口
```

### 4.1 API 客户端（`api.ts`）

- `BASE = '/api'`，统一走 Vite dev proxy 或同源部署。
- 按资源分组导出：`sessions`、`files`、`info`、`skills`、`mcp`、`config`、`teams`。
- `connectStream(sessionId, onEvent, onClose)` 创建 `WebSocket`：自动根据 `window.location.protocol` 选 `ws:` / `wss:`，每条消息 `JSON.parse` 后回调成 `AgentEvent`。

### 4.2 主要组件（均在 `App.tsx` 内）

- **`App`**（默认导出）：会话列表 + 当前会话状态 + 输入框 + 事件处理 `handleEvent`。包含一个左侧可拖拽宽度的 sidebar，`sidebarTab` 在 `sessions` / `hooks` / `skills` / `mcp` 等几个 tab 间切换。
- **`PermissionModal` / `AskUserModal`**：消费 `permission.requested` / `ask_user.requested` 事件并 POST 回 `/resolve` 或 `/answer`。
- **`ToolCard`**：渲染单次工具调用（status 颜色：running 黄色脉冲、error 红、成功绿），可折叠展开输入/输出。
- **`SessionItem`**：单条会话条目，支持重命名、删除、置顶切换。
- **`SkillsPanel`**：CRUD + 启用/禁用 + 上传 SKILL.md 自动解析 frontmatter；区分只读源（`skills/`）和可写运行时（`.mycodeagent/skills/`）。
- **`McpPanel`**：CRUD + 启用/禁用 + 支持 `mcpServers` JSON 粘贴导入。
- **`HooksPanel`**：读写 `.mycode/hooks.json`。
- **`NavItem`**：sidebar 左侧导航条目。

状态管理刻意全部用 React 原生 `useState` / `useEffect` / `useRef`，没有引入 Redux/Zustand 等库，理由是状态总量小、且大部分由 WebSocket 事件驱动逐步累加。

## 5. 会话隔离

每个会话有三道隔离：

1. **运行态**：`AgentSession` 是一个独占 daemon Thread + 独占 `queue.Queue[AgentEvent]` + `_busy` 互斥锁。`send_message` 在 busy 期间直接返回 False（前端收到 409）。
2. **工作空间**：`AgentSession._ensure_agent` 在 `MYCODEAGENT_DATA_DIR/sessions/<sid>/` 下创建一个目录，然后调用 `agent._rebind_project_root(workspace)`，把 `project_root`、权限网关、所有工具的 `_project_root` / `_working_dir`、`context_builder.project_root`、`SkillLoader` 全部重绑到该目录。后续文件读写、上传、下载都被限制在这里。
3. **持久化**：`core/session_manager.py` 把 `SessionInfo`（id、title、preview、message_count）写到 `memory/sessions/index.json`，快照写成临时文件后 `os.replace` 原子替换。前端 `GET /api/sessions` 把内存活跃 session 与磁盘持久化 session 合并展示，给磁盘上的会话提供 `POST /activate` 重新拉起 worker 线程。

注意：`.mycode/hooks.json` 目前仍跟随 **项目根目录**（即启动后端时的 cwd），不是 per-session 的——`HooksPanel` 编辑的是整个项目共享的 hook 配置，所有会话生效。`.teams/`、`.tasks/` 同理仍绑定到 TeamManager 创建时的根（参见 `2026-06-26-team-engine-production-hardening.md` 中标记的 P2 项）。

## 6. 文件上传 / 下载

**上传**：前端拖拽或点击触发 `POST /api/sessions/{sid}/upload`（multipart），后端把文件写入 `workspace_dir`，返回 `{path, name, size}`。Agent 即可以在后续轮次里 `Read` 这些文件，因为它们已经落在会话的 `project_root` 内。

**下载**：所有工具的输出经 `ObservationTruncator` 过截断，超过 `TOOL_OUTPUT_MAX_LINES`（默认 2000）或 `TOOL_OUTPUT_MAX_BYTES`（默认 50KB）的部分会被持久化到 `tool-output/tool_<ts>_<tool>.json`。前端通过 `GET /api/sessions/{sid}/files/download?path=...` 把这些文件以 `application/octet-stream` 拉回本地。`tool-output/` 默认 7 天保留期（`TOOL_OUTPUT_RETENTION_DAYS`），每次写入有 10% 概率触发清理。

下载路径同样会做 `.relative_to(session.workspace_dir)` 检查（修复在 commit `012b020`：之前用的是 `agent.project_root`，会随 `EnterWorktree` 漂走）。

## 7. Hooks 管理面板

`HooksPanel`（`App.tsx:470`）通过两个接口工作：

- `GET /api/hooks` → 读取 `.mycode/hooks.json`（文件不存在时返回 `{hooks: {}}`）
- `PUT /api/hooks` → 写回完整文档

前端以纯 JSON 编辑器形式展示，校验交给后端（写入失败返回 4xx）。Hook 引擎本身由 `core/features/hooks.py::HookFeature`（order=85）管理：

- `post_init` 触发 `SessionStart`
- `pre_tool_use` / `post_tool_use` 在工具执行前后回调
- `cleanup` 触发 `SessionEnd`

因此面板上的修改对**下一个新建的会话**立即生效；已运行中的会话保留启动时的快照。

## 8. Skill / MCP 面板

### 8.1 SkillsPanel

`SkillLoader`（`core/skills/skill_loader.py`）是两层结构：

- `skills/`（源，git 跟踪，**只读**，靠 `s.base_dir.startsWith('skills/')` 判定）
- `.mycodeagent/skills/`（运行时，可写；第一次扫描时 seed 自源目录，运行时同名条目覆盖源条目）

前端能力：

- 列表 + 启用状态（`enabledMap` 来自 `GET /api/skills/{name}/enabled`，由 `skill_state.json` 持久化）
- 上传 `SKILL.md` 自动解析 YAML frontmatter（`name`、`description`），填充表单
- `validate` → `create` 两步：先 `POST /api/skills/validate` 做命名正则 + 字段非空校验，通过后再 `POST /api/skills` 真正落盘
- 源 skill 被标记为只读，禁用编辑/删除按钮

### 8.2 McpPanel

读写 `mcp_servers.json`，支持两种输入模式：

- **Form 模式**：name / command / args（空格分割）
- **JSON 模式**：直接粘贴 `{"mcpServers": {...}}` 块，前端 `validateJson` 提取首个 server 后调 `POST /api/mcp/servers`

Enabled 状态独立持久化，便于在不删除配置的情况下临时停用某个 server。`GET /api/mcp/status` 报告实际连接状态（servers、pending、`connect_mode`），便于在 `manual` 模式下排查"配置存在但未连接"的情况。

## 9. 与 TUI 的关系

TUI 与 Web 是同一份 agent 核心的两个并列消费者。对比：

| 维度 | TUI（`scripts/chat_test_agent.py`） | Web（`desktop/`） |
|---|---|---|
| 是否使用 SessionController | 否，主线程直接 `agent.run()` | 是，每会话一个 worker 线程 |
| 权限询问 | `PermissionDialog.ask` 接到 `_permission_gate._broker` | `_SessionPermissionBroker` 推事件 → 浏览器弹窗 → POST 回 resolve |
| AskUser | `session.prompt(HTML(...))` 接到 `ask_tool._input_func` | `_SessionAskUserFunc` 推事件 → 浏览器弹窗 → POST 回 answer |
| 流式 | `tui/streaming.py` Rich Live 增量更新 | `llm.completed` / `assistant.final` 事件经 WebSocket 推前端 |
| 会话数 | 单进程单会话（`/sessions`/`/resume` 切换快照） | 多会话并发，每个会话独立线程 |
| 状态共享 | 同进程内存 | 磁盘快照 + `controller` 字典 |

两者**完全可以同时运行**（不同进程），但都会持久化到 `memory/sessions/`，因此重启后能跨 UI 看到对方留下的会话快照。

## 10. 启动方式

### 后端

```bash
# 假定项目根目录
uvicorn desktop.service.app:create_app --factory --host 0.0.0.0 --port 8000

# 或自定义 data 目录（默认 .mycodeagent/）
MYCODEAGENT_DATA_DIR=./my-data uvicorn desktop.service.app:create_app --factory --port 8000
```

`create_app` 接收一个 `agent_factory: Callable[[], CodeAgent]`，默认工厂会按当前环境变量构造一个 `CodeAgent`。

### 前端

```bash
cd desktop/web
npm install
npm run dev      # vite dev server, 默认 5173, 代理 /api -> 后端
npm run build    # 输出到 desktop/web/dist/
```

桌面壳（Tauri / Electron）方案当前未落地，预留位：把 `dist/` 静态文件由后端兜底 serve，或用 Tauri 把后端打包成 sidecar 进程。

## 11. 后续规划

- **鉴权**：默认仅本机的现状要在远程暴露场景下补一层 Token / Origin 校验。
- **WebSocket 双向化**：把 `interrupt`、流量背压等控制信号从 REST POST 改到 WS，减少建立连接的开销并支持 server push 之外的实时控制；同时把流式 LLM 的 `reasoning` delta 与 `tool_call` delta（参见 LLM Streaming 设计 P1 未完成项）一并推到前端的工具卡片上做实时渲染。
