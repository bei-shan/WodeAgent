# Phase 5 — 子代理流式（Subagent Streaming）设计文档

- 日期：2026-06-29
- 状态：草案（待维护者审阅）
- 关联：`docs/design/2026-06-26-llm-streaming-design.md`（P1 已完成，本文档对应其 "后续路线" 中 Phase 5 的第 2 项）

---

## 1. 背景与目标

### 1.1 什么是 subagent streaming

主代理在 P1 阶段已经接入 LLM 流式：用户能在 TUI/Web 上看到主 Agent 的 token 逐字渲染（`core/llm.py:560-637` + `agents/codeAgent.py:818-948` + `tui/streaming.py:16-93`）。但只要主 Agent 调用 `Task` 工具派发一个子代理（subagent），UI 就会"哑火"——子代理同步阻塞地跑完整个 ReAct 循环，主 Agent 才能继续，期间 UI 完全看不到子代理在做什么。

**subagent streaming** 就是要让子代理的执行过程也能实时回流到 UI：包括它正在调哪个工具、当前 LLM 在吐什么 token、跑到第几步、何时结束。

### 1.2 不做这件事会失去什么

具体场景：

1. **后台 Task 黑盒**：用户用 `Task(run_in_background=True)` 派发一个长任务，今天唯一的可见性只有 `BackgroundTaskRunner.summary_text()`（`core/background_task.py:162-188`），它每个 ReAct step 把 `[Background Tasks] ⏳ description` 一行字塞进主 Agent 的 system prompt。这是给 LLM 看的，**TUI 和 Web 都看不到**。`TaskOutput` 工具（`tools/builtin/task_output.py:46-107`）对运行中任务也只返回 `{status: running, elapsed: Ns}`，根本不调 `get_progress()`，所以主 Agent 也不知道子代理在干嘛。
2. **同步 Task 长时间等待**：`tools/builtin/task.py:482-531` 的 oneshot 路径完全阻塞，期间 TUI 只在 `scripts/chat_test_agent.py:248-261` 打印一行 `⚡ Team Dispatch Task` 就没下文，子代理可能跑十几秒甚至几分钟，用户面对一片空白。
3. **Team Engine 多 worker 看不到分工**：`core/team_engine/turn_executor.py:35` 仍是 `self.llm.invoke_raw(...)`，每个 teammate 一轮发言都得等整个轮次完成。今天唯一的"并行 UI"是 OS 级 tmux 分屏（`core/team_engine/display_mode.py:10-28`），Rich 内并没有多面板能力。
4. **Web 端 WebSocket 帧稀疏**：`desktop/service/app.py:69-82` 的 `_STREAM_EVENTS` 只放行 12 种事件，子代理整个生命周期对前端是"一片寂静"。

---

## 2. 现状盘点

### 2.1 子代理今天怎么跑

两条派发路径，都不走主 Agent 的事件总线：

**同步 `Task`**（`tools/builtin/task.py:482-531`）：
- `TaskTool.run` 直接构造 `SubagentRunner(llm, tool_registry, system_prompt, project_root, max_steps)`（`tools/builtin/task.py:166-189`），其 `__init__` **不接受 `event_sink` 参数**。
- 内部调用 `TurnExecutor` 跑 ReAct，最终返回字符串。
- 仅有的过程钩子是 `progress_callback(step, event_type, data)`（`tools/builtin/task.py:192-249`），事件类型只有 `action/thought/error` 三种，且 content 被截断到 200 字符——**这个回调今天在 TaskTool 调用方被默默丢弃**。

**后台 `Task`**（`tools/builtin/task.py:450-480` + `core/background_task.py`）：
- `BackgroundTaskRunner.launch` 起一条 daemon 线程（`core/background_task.py:122`，`threading.Thread(target=_run, name=f"BgTask[{task_id[:8]}]", daemon=True)`）。
- 唯一的线程间协同原语是 `self._lock = threading.Lock()`（`core/background_task.py:44`），保护 in-memory 的 `self._tasks` 字典。**没有** per-task 的 `Event/Condition/Queue/观察者列表`。
- 进度只通过磁盘 JSONL 回流：`_progress_callback`（`core/background_task.py:87-97`）把每一步追加到 `.tasks/progress/{task_id}.jsonl`，完成时写 `{"type": "done"}` 哨兵行（`core/background_task.py:114-120`）。
- 结果走 `.tasks/output/{task_id}.json`，原子 `tmp -> rename`（`core/background_task.py:244-245`）。
- 没有 `stop()/cancel()/join()`：`clear_completed()`（`core/background_task.py:190-198`）只清理 in-memory 跟踪，**不通知/不终止线程**。

### 2.2 主代理流式已经成熟，但子代理还在用 `invoke_raw`

主 Agent 的流式链路：`core/llm.py:560-637 stream_raw` → `on_delta(event_type, text)` → `agents/codeAgent.py:840-843` 转发给 `self.llm_stream_callback(event_type, text, step)` → `scripts/chat_test_agent.py:681` 写入 `StreamingResponse`。

子代理两个 LLM 入口都没接 stream：
- `core/team_engine/turn_executor.py:35` 直接 `self.llm.invoke_raw(...)`。
- `tools/builtin/task.py` 走 `SubagentRunner` → `TurnExecutor`，因此一样是 `invoke_raw`。

`docs/design/2026-06-26-llm-streaming-design.md:130-133` 明确把 "Team Engine TurnExecutor 接入流式" 列为 Phase 5 待办。

### 2.3 TUI / Web 怎么看子代理

TUI（`scripts/chat_test_agent.py`）：
- 直接实例化 `RichConsoleCodeAgent`（line 490），**不走 `SessionController`**，不读 `events` 队列；用的是 `_console()` 旧回调 + emoji 前缀模式匹配（`scripts/chat_test_agent.py:116-202`）。
- 调子代理时只打印 `⚡ Team Dispatch Task` 一行（`scripts/chat_test_agent.py:248-261`），然后阻塞等结果。
- `StreamingResponse` 是单例：`scripts/chat_test_agent.py:592` 全进程只 new 一个；line 667-669 警告 "Live 期间禁止 `console.print()`，否则错位"。**Rich Live 在同一 Console 内并发本就是反模式。**

Web（`desktop/service/app.py:507-549`）：
- WebSocket `/api/sessions/{sid}/stream` 用 `loop.run_in_executor` 把 `session.events.get` 桥到异步。
- `_STREAM_EVENTS`（`desktop/service/app.py:69-82`）只放行 8 个 `EventType.*` + 4 个 session 事件——**没有任何 `subagent.*` 事件可订阅**。
- `desktop/web/src/App.tsx:630-660` 的 `handleEvent` switch 也没有 subagent 分支。

`TaskOutput` 工具（`tools/builtin/task_output.py:46-107`）是唯一查询后台状态的工具，运行中的任务它也只返回 `{status: running, elapsed: Ns}`（line 66-78），**完全没调 `BackgroundTaskRunner.get_progress()`**，主 Agent 因而无法回灌子代理的中间进度。

### 2.4 事件系统现状

`core/events.py:29-42` 共 10 个 `EventType` 常量，主 Agent 在 `agents/codeAgent.py:557, 598, 680, 719, 828, 880, 1001, 1028` 发出 8 个。`SessionController._ensure_agent`（`core/runtime/session_controller.py:180`）懒装一个 `_QueueEventSink`（line 277-284）把事件推到 `queue.Queue`。`AgentEvent`（`core/events.py:47-55`）只有 `type/payload/step` 三个字段，**没有 `subagent_id`**。

---

## 3. 设计目标 & 非目标

### 3.1 目标（可验证）

1. **G1 — 子代理 LLM token 实时回流**：子代理（同步 `Task`、后台 `Task`、Team `TurnExecutor`）调用 LLM 时，token 必须通过事件总线推送到 UI，端到端延迟 ≤ 主 Agent 流式同一数量级（10 fps 渲染粒度）。
2. **G2 — 子代理工具调用可观测**：子代理调用任何工具时，UI 都能看到 `tool.started/tool.completed` 等价事件，并能通过 `subagent_id` 与父 step 关联（用于折叠/缩进渲染）。
3. **G3 — Web 与 TUI 共用同一事件契约**：新增的 `subagent.*` 事件类型走 `EventSink`，Web 通过 `_STREAM_EVENTS` 白名单自动透传，TUI 也能订阅同一组事件——不再让 TUI 走 `_console` emoji 解析。
4. **G4 — 后台任务可查中间态**：主 Agent 通过 `TaskOutput` 能看到运行中子代理的 "当前正在做什么"（last_step / current_tool / 最近一条进度），实现"主 Agent 也能感知到 background 子代理的脉搏"。
5. **G5 — 并发安全**：多个 subagent 同时流式时，事件互不污染；UI 端能可靠按 `subagent_id` 路由到正确的子面板/子缓冲区。

### 3.2 非目标

- **NG1 — 不做硬取消（stop/cancel）**：daemon 线程的中断语义复杂（`core/background_task.py` 今天 `_run()` 跑完才退出，没有 `Event` 检查），本期只做"可观测"，不做"可控"。`clear_completed` 行为保持不变。
- **NG2 — 不重写 TUI 渲染层为多面板 Rich Layout**：本期 TUI 端只做"事件订阅"接入和"子代理进度行"打印，不做并行多 Panel 渲染（Rich Live 同 Console 并发是已知反模式，留待后续）。Web 端因为是 React 多组件，天然支持并发渲染。
- **NG3 — 不改 `Task` 工具的对外 schema**：`Task(prompt, description, run_in_background, ...)` 保持兼容；新增能力通过隐式的 `event_sink` 注入（DI 容器）实现，**LLM 看到的工具签名零变化**。
- **NG4 — 不改 `progress_callback` 在 `BackgroundTaskRunner` 内部的 JSONL 写盘**：磁盘 JSONL 仍是真相源（崩溃恢复 / 跨进程查询 / VCR 都依赖），只在写盘的同一注入点**额外**走一份内存 fanout。
- **NG5 — 不在本期处理 reasoning 流**：保留主 Agent 现状（CLI 仍过滤掉 `reasoning` 事件）；子代理 `reasoning` 默认也透传到 sink，但 TUI 是否渲染 thinking 块属于另一独立 Phase。

---

## 4. 总体架构

### 4.1 数据流（ASCII）

```
                       === 子代理进程内（daemon 线程 / 主线程 oneshot）===
   ┌──────────────────────────────────────────────────────────────────┐
   │  TurnExecutor.execute_turn          [改：invoke_raw → stream_raw] │
   │     │                                                            │
   │     ▼                                                            │
   │  HelloAgentsLLM.stream_raw(on_delta=_subagent_on_delta)  [已有]  │
   │     │  fires on_delta("content"|"reasoning", text)               │
   │     ▼                                                            │
   │  SubagentEventBridge(subagent_id, parent_step, sink)     [新增]  │
   │     │   .emit_llm_delta(text, kind)                              │
   │     │   .emit_tool_started(name, args)                           │
   │     │   .emit_tool_completed(name, ok, summary)                  │
   └─────┼────────────────────────────────────────────────────────────┘
         │
         │ AgentEvent("subagent.delta"|"subagent.tool_*"|..., {subagent_id, ...})
         ▼
   ┌─────────────────────────────────────┐
   │ EventSink (注入自父 agent)          │   [已有：core/events.py:61-70]
   └─────┬───────────────────────────────┘
         │
         ├─► 同步 Task 路径：父 agent.event_sink 直接收            [已有 sink，新增子代理事件类型]
         │
         └─► 后台 Task 路径：BackgroundTaskRunner 的 fanout sink   [新增：内存观察者]
                 │           （同时仍写 .tasks/progress/{id}.jsonl）
                 ▼
            sink.emit(AgentEvent("subagent.delta", {task_id, ...}))
                 │
                 ▼
   ┌─────────────────────────────────────┐
   │ AgentSession.events: queue.Queue    │   [已有：core/runtime/session_controller.py:78]
   └─────┬───────────────────────────────┘
         │
         ├─► Web: WS /api/sessions/{sid}/stream                  [已有，新增 _STREAM_EVENTS 条目]
         │      → React handleEvent(case 'subagent.*')           [新增分支]
         │
         └─► TUI: 新增 TuiEventSubscriber（订阅同一 queue 子集） [新增小组件]
                 → console.print 单行子代理进度（不动 Rich Live）
```

### 4.2 哪些是新代码、哪些是已有代码

| 模块 | 状态 | 说明 |
|------|------|------|
| `core/llm.py:stream_raw` + `_merge_streaming_chunks` | **已有，零改动** | 子代理复用同一流式入口 |
| `agents/codeAgent.py:_invoke_llm_with_retry` 的 stream 分支 | **已有，零改动** | 主 Agent 路径不变 |
| `core/team_engine/turn_executor.py:35` | **改造** | `invoke_raw` → `stream_raw`，加 `on_delta` 参数 |
| `tools/builtin/task.py` SubagentRunner.__init__ | **改造** | 新增 `event_sink`、`subagent_id`、`parent_step` 参数 |
| `tools/builtin/task.py` TaskTool 派发点 (line 454-490) | **改造** | 从 DI 容器拿父 sink 注入下去 |
| `core/events.py:EventType` | **改造** | 新增 4 个常量：`SUBAGENT_STARTED/DELTA/TOOL/FINISHED` |
| `core/events.py:AgentEvent` | **改造** | 新增 `subagent_id: Optional[str] = None`（向后兼容默认值）|
| `SubagentEventBridge` | **新增** | 30~60 行小类，把 `on_delta` 和 `progress_callback` 翻译成 `AgentEvent` |
| `core/background_task.py` | **改造** | `_progress_callback` 内额外 fanout 给注册的内存观察者 |
| `core/background_task.py:get_status` | **改造** | 增加 `last_step / current_tool / last_event` 派生字段（读 JSONL tail 或 in-memory） |
| `core/features/background_task.py` | **改造** | 在 init 阶段把 `agent.event_sink` 注册成观察者 |
| `tools/builtin/task_output.py:66-78` | **改造** | 运行中分支调用 `get_progress(since_step)`，把摘要回给 LLM |
| `desktop/service/app.py:69-82 _STREAM_EVENTS` | **改造** | 加 4 个新事件类型 |
| `desktop/web/src/App.tsx:630-660 handleEvent` | **改造** | 新增 `subagent.*` 分支 |
| TUI 订阅器 | **新增（小）** | 一个轻量 `EventSink` 子类，print 单行子代理进度（不与 Rich Live 冲突）|

---

## 5. 接口设计

### 5.1 `EventType` / `AgentEvent` 扩展（`core/events.py:29-42, 47-55`）

新增 4 个常量：

```python
class EventType:
    # ... 原 10 个保持不变 ...
    SUBAGENT_STARTED   = "subagent.started"
    SUBAGENT_DELTA     = "subagent.delta"     # 一次 LLM token 增量
    SUBAGENT_TOOL_USE  = "subagent.tool_use"  # 工具开始/结束（用 payload.phase 区分）
    SUBAGENT_FINISHED  = "subagent.finished"
```

`AgentEvent` 加一个可选字段：

```python
@dataclass
class AgentEvent:
    type: str
    payload: dict[str, Any] = field(default_factory=dict)
    step: int = 0
    subagent_id: Optional[str] = None  # 新增；None 表示主 Agent
```

`subagent_id` 命名规则：
- 同步 `Task`：`task-{8 位 uuid 前缀}`，每次 `TaskTool.run` 生成一次。
- 后台 `Task`：直接复用 `BackgroundTaskRunner` 已经生成的 `task_id`（`tools/builtin/task.py:454`），无需新增。
- Team Engine teammate：`team-{team_name}-{worker_index}`（Phase 2 落地时可只先做单 worker，留 hook）。

### 5.2 `BackgroundTaskRunner` 新增字段/方法（`core/background_task.py`）

不破坏现有公开 API。新增：

```python
class BackgroundTaskRunner:
    def __init__(self, project_root: Path):
        # ... 现有 ...
        self._observers: list[Callable[[str, dict], None]] = []   # 新增

    def register_observer(self, callback: Callable[[str, dict], None]) -> None:
        """注册一个 (task_id, progress_record) 的内存回调；线程安全，调用方异常不影响磁盘写。"""

    def unregister_observer(self, callback: Callable[[str, dict], None]) -> None:
        ...

    # 改造点：_progress_callback 内在写完 JSONL 后，遍历 observers 调用（异常吞掉）
```

`get_status` 扩展为返回：

```python
{
    "status": "running" | "completed" | "failed",
    "started_at": float,
    "elapsed": float,
    # 新增（仅 running 时）：
    "last_step": int,
    "current_tool": Optional[str],
    "last_event": Optional[dict],  # 最近一条 JSONL 记录的浅拷贝
}
```

派生字段的实现可在 `self._tasks[task_id]` 上加 `last_progress` 字典（在 `_progress_callback` 内同步更新，复用现有 `self._lock`），避免每次都去读 JSONL。**绝不引入新锁**：复用 `self._lock`。

### 5.3 `Task` 工具的 API

**对 LLM 暴露的 schema 零变化**——`Task(prompt, description, run_in_background, ...)` 完全兼容（NG3）。

内部签名变化（`tools/builtin/task.py:166-189`）：

```python
class SubagentRunner:
    def __init__(
        self,
        llm,
        tool_registry,
        system_prompt,
        project_root,
        max_steps,
        event_sink=None,         # 新增
        subagent_id=None,        # 新增
        parent_step=0,           # 新增；用于 AgentEvent.step
    ): ...
```

`TaskTool._run_oneshot` / `_run_in_background` 通过 DI 容器（`agents/codeAgent.py:190` 的 `bootstrap.provide`）拿到父 agent 的 `event_sink`，构造 `SubagentRunner` 时传入。

### 5.4 `SubagentEventBridge`（新增，约 60 行，放 `tools/builtin/task.py` 同文件或 `core/runtime/subagent_bridge.py`）

```python
class SubagentEventBridge:
    """把 SubagentRunner 内部的 on_delta / progress_callback / tool 事件
    统一翻译成 AgentEvent，推到父 sink。线程安全（无共享可变状态，仅调用 sink.emit）。"""

    def __init__(self, sink: EventSink, subagent_id: str, parent_step: int): ...

    def emit_started(self, description: str, prompt_preview: str) -> None: ...
    def emit_llm_delta(self, kind: str, text: str) -> None:
        # kind ∈ {"content", "reasoning"}；payload = {"kind": kind, "text": text}
    def emit_tool(self, phase: str, name: str, args: dict, result_preview: str = "") -> None:
        # phase ∈ {"started", "completed"}
    def emit_finished(self, ok: bool, result_preview: str, steps: int) -> None: ...
```

每个 `emit_*` 都附带 `subagent_id`；`AgentEvent.step` 用 `parent_step`（保持主 Agent step 流单调，子代理事件按 `subagent_id` 分组而非 step 重排）。

### 5.5 `TUI` 如何订阅

TUI 当前不走 `SessionController`，但 `RichConsoleCodeAgent` 已经持有 `agent.event_sink`（默认 no-op）。本期方案：

1. 提供一个 `TuiSubagentRenderer(EventSink)`（新增小类，约 40 行）。
2. 在 `scripts/chat_test_agent.py` 启动时把它装到 `agent.event_sink`——**但只在主 Agent 的 Rich Live 不活动时**才 `console.print`；Live 活动期间事件先入内存缓冲，`stream.finish()` 后 flush（参照 line 204-246 `flush_console_buffer` 的现有模式）。
3. 不引入 Rich 多 Panel Live（NG2）。子代理事件以单行紧凑格式打印：`[subagent task-3f2a1c] ⚙ Read(file=...)` / `[subagent task-3f2a1c] … 12 tokens` 等。

Web 端通过 `_STREAM_EVENTS` 自动透传，前端 `handleEvent` 新增 `case 'subagent.started'/'delta'/'tool_use'/'finished'`：在主 Agent 消息卡片下方挂一个折叠子卡片，按 `subagent_id` 分组。

---

## 6. 实施步骤

按 4 步推进，每步独立可测试可发布。对应 Phase 5 todo.md 中 Steps 25-28。

### Step 25 — 事件契约扩展 & TurnExecutor 流式化

- **改动文件**：
  - `core/events.py`（新增 4 个 EventType + `AgentEvent.subagent_id` 字段）
  - `core/team_engine/turn_executor.py`（`invoke_raw` → `stream_raw`，新增 `on_delta` 参数）
  - 新增 `tools/builtin/_subagent_bridge.py`（或同文件内私有类）
- **完成态**：
  - `EventType.SUBAGENT_*` 4 个常量存在；`AgentEvent(..., subagent_id="x")` 可构造且向后兼容。
  - `TurnExecutor.execute_turn` 支持可选的 `on_delta` / `event_bridge` 参数；不传时行为与今天完全一致（仍走 `invoke_raw` 或在 LLM 不支持 `stream_raw` 时降级，参考 `agents/codeAgent.py:833-856` 的现有降级模式）。
  - 单元测试：用 `FakeLLM` 验证 `on_delta` 被调用 N 次、最终结果仍 = `invoke_raw` 等价响应（复用 `_merge_streaming_chunks`，无需重造）。
- **测试策略**：纯单测；`tests/test_team_turn_executor_streaming.py` 新增（无外部依赖）。
- **风险**：`stream_raw` 在某些 provider 上需 `stream_options` 自动降级（`core/llm.py:591-606`）——已在主 Agent 路径验证过，子代理路径直接复用同一调用栈即可，风险低。

### Step 26 — 同步 Task 路径接入事件总线

- **改动文件**：
  - `tools/builtin/task.py`（`SubagentRunner.__init__` 新增 3 参数；`_run_oneshot` 从 DI 容器取 sink；构造 `SubagentEventBridge` 并喂给 `SubagentRunner.run`）
  - `agents/codeAgent.py:190` 附近（确认 `event_sink` 已通过 `bootstrap.provide` 暴露；若没有则补上 `bootstrap.provide("event_sink", self.event_sink)`）
  - `core/tool_bootstrap.py`（在 `TaskTool` 注入处加 `event_sink` DI）
- **完成态**：
  - 同步 `Task` 调用过程中，父 sink 收到一序列：`subagent.started` → 多次 `subagent.delta` + `subagent.tool_use` → `subagent.finished`。
  - `subagent_id` 正确填充，`parent_step` 与父 Agent 当前 step 一致。
- **测试策略**：
  - 集成测试：在 `tests/test_subagent_streaming.py` 内 mock 一个父 sink（list-recording `EventSink`），跑一个简短 `Task` prompt，断言事件序列。
  - 回归：跑 `pytest tests/test_*task*.py` 全绿。
- **风险**：
  - DI 容器拿到的 sink 可能是 default no-op（headless 场景）——这是预期行为，no-op sink 不应崩溃，单测覆盖。
  - 父 Agent 的 step 计数在子代理执行期间可能继续推进——本期固定取调用点的 `parent_step`（不动），子代理事件 `step` 字段全部用这一个值，UI 按 `subagent_id` 而非 `step` 分组。

### Step 27 — 后台 Task 路径接入 + TaskOutput 中间态

- **改动文件**：
  - `core/background_task.py`（`_observers` 列表 + `register_observer/unregister_observer`；`_progress_callback` fanout；`get_status` 派生字段 `last_step/current_tool/last_event`；`self._tasks[task_id]["last_progress"]` 在锁内更新）
  - `core/features/background_task.py`（在 init 时 `runner.register_observer(self._fanout_to_sink)`；cleanup 时 unregister）
  - `tools/builtin/task_output.py:66-78`（运行中分支调用 `runner.get_status` 拿派生字段，必要时 `runner.get_progress(since_step=N)` 取最近 K 条，返回给 LLM 一段紧凑文本）
- **完成态**：
  - 后台 `Task` 期间，父 sink 持续收到 `subagent.delta/tool_use` 事件。
  - 主 Agent 调 `TaskOutput(task_id)` 时，对运行中任务能拿到 `last_step=N, current_tool=Read, last_thought="..."` 等可读摘要。
  - 磁盘 JSONL 行为零变化（NG4）；observer 异常被吞掉，**不能阻塞磁盘写**。
- **测试策略**：
  - 单测：`tests/test_background_task_observer.py` — 注册 observer，launch 一个 fake `_run`（直接调几次 `_progress_callback`），断言 observer 收到事件 + JSONL 仍正确写入 + observer 抛异常时主流程不挂。
  - 集成：复用现有的后台 Task 测试场景，加一条 "主 Agent 第 N 步调 TaskOutput，能看到 current_tool" 的断言。
- **风险**：
  - **跨线程 sink.emit 的线程安全**：详见 §8.1。`_QueueEventSink.emit` 只是 `Queue.put`（`core/runtime/session_controller.py:277-284`），`queue.Queue` 本身线程安全；no-op `EventSink.emit` 也无状态。**这是当前唯一被注入的两种 sink，都是天然线程安全的**，但本期要在文档/代码注释里明确写出"`EventSink.emit` 实现必须线程安全"作为契约，避免后续踩坑。
  - observer 列表的并发修改：`register/unregister` 与 `_progress_callback` 遍历都用 `self._lock` 保护即可。

### Step 28 — Web/TUI 渲染 + 端到端验收

- **改动文件**：
  - `desktop/service/app.py:69-82`（`_STREAM_EVENTS` 加 4 个新常量）
  - `desktop/web/src/api.ts:14`（`AgentEvent` interface 加 `subagent_id?: string`）
  - `desktop/web/src/App.tsx:630-660`（`handleEvent` 加 `case 'subagent.started/delta/tool_use/finished'`，按 `subagent_id` 维护 `Map<subagent_id, SubagentCard>`，渲染折叠卡片）
  - 新增 `tui/subagent_renderer.py`（`TuiSubagentRenderer(EventSink)`，约 40 行）
  - `scripts/chat_test_agent.py:592` 附近（在 `RichConsoleCodeAgent` 上装 sink；Live 期间走内存缓冲，`stream.finish()` 后 flush，复用现有 `flush_console_buffer` 模式）
- **完成态**：
  - Web：派发 Task 后，主消息下方出现 "Subagent task-xxxxx ⚙ Running" 折叠卡片，展开后看到 token 实时滚动、工具调用列表；finished 后变成绿色对勾。
  - TUI：派发 Task 后，主 Agent Live 结束（或在 oneshot 内每个 step 之间）会插入子代理紧凑进度行；不破坏 `tui/streaming.py:16-93` 单 Live 的工作模式。
- **测试策略**：
  - 端到端手动 §7 场景；
  - 前端：snapshot 测试 `handleEvent` 对 subagent 事件的 reducer；
  - TUI：断言 `TuiSubagentRenderer` 在 Live 活动期间不直接 print，缓冲后 flush。
- **风险**：见 §8.4。

---

## 7. 验收标准

### 7.1 同步 Task 端到端（Web）

启动 `uvicorn desktop.service.app:create_app --factory --reload` + `desktop/web` 前端，新会话发送："请用 Task 工具帮我读 `core/events.py` 并总结其结构"。

期望：
- 主 Agent 消息流中出现 `Task` 工具卡片（已有）。
- 卡片下方出现新的子代理折叠卡片，标题含 `subagent_id`。
- 展开卡片：看到子代理 LLM token 逐字滚动（与主 Agent 的 fps 同量级），看到 `Read(core/events.py)` 工具行 started → completed。
- 子代理结束时卡片变为 `finished ok=true`，主 Agent 继续。

### 7.2 后台 Task 中间态（任意 UI）

主 Agent 调 `Task(prompt="…", run_in_background=True)`，2 秒后主 Agent 主动调 `TaskOutput(task_id=...)`。

期望：返回内容包含 `status=running, last_step≥1, current_tool=<某真实工具名>, last_event=<JSON 片段>`——而不是今天的 `{status: running, elapsed: Ns}` 干瘪结构。

### 7.3 Web 实时观看后台 Task

Web 会话内发起一个后台 `Task`，主 Agent 继续响应别的话题；前端在主消息流之外的"后台任务"区/折叠区能看到 `subagent.delta` 持续打字、`subagent.tool_use` 持续插入工具行，无须刷新页面。

### 7.4 多个 subagent 并发不串

构造一条 prompt 让主 Agent 同时派发两个后台 `Task`。前端两个子代理卡片各自的 token 流不混淆（按 `subagent_id` 路由），最终 `BackgroundTaskRunner.get_progress` 读到的两份 JSONL 也分别对应。

### 7.5 兼容性

- `LLM_STREAMING=false` 时，子代理路径自动降级回 `invoke_raw`，仅发 `subagent.started/finished`（不发 `delta`），UI 仍能看到事件序列。
- 默认 no-op `EventSink` 下（裸 `CodeAgent(...)` 用例 + headless 脚本），子代理事件 emit 零开销且不崩溃。
- 跑完整测试套件（按 CLAUDE.md 提供的 pytest 命令）无回归。

---

## 8. 风险与未决问题

### 8.1 线程安全：daemon 子代理跨线程写 `event_sink`

- **风险**：`BackgroundTaskRunner` 在 daemon 线程内调 `_progress_callback`，fanout 后会调 `sink.emit`。如果某天有自定义 `EventSink` 持有可变状态，并发 emit 可能踩。
- **缓解**：
  1. 在 `core/events.py` 注释里把"`EventSink.emit` 必须线程安全"写成契约。
  2. 现有两个实现都天然线程安全（no-op + `queue.Queue.put`），目前无破坏。
  3. `SubagentEventBridge` 自身无可变状态，方法纯转发。
  4. observer 异常吞掉（try/except + logger.warning），保证 JSONL 主路径不挂。

### 8.2 并发：多个 subagent 同时流式

- **风险**：UI 端如何区分；`AgentEvent.step` 是否会冲突。
- **方案**：
  - 所有子代理事件必须带 `subagent_id`；UI（Web `handleEvent`、TUI renderer）一律按 `subagent_id` 分桶，**不依赖 step**。
  - `BackgroundTaskRunner` 已经天生支持多 task（in-memory dict + 各自的 JSONL 文件），fanout 时把 `task_id` 当作 `subagent_id` 透传即可。

### 8.3 性能：序列化 chunk 频率

- **风险**：每个 LLM token 都发一个 `AgentEvent`，Web 端 WebSocket 帧速率可能爆掉。
- **粗算**：DeepSeek/GLM 100~200 tok/s 量级，单子代理峰值约 200 帧/s，单帧 payload < 100 字节 → ~20 KB/s，WebSocket 完全承受得住。10 个子代理并发 ≈ 200 KB/s，仍在合理范围。
- **缓解（可选）**：可在 `SubagentEventBridge.emit_llm_delta` 里做 8~16ms 合并（在线程本地累计 buffer，定时 flush），但**默认不上**，先用最朴素的逐 token emit 看真实数据。
- **`_sanitize_payload`**（`desktop/service/app.py:1053-1065`）已经会截断 >50KB 的字符串，无需特殊处理。

### 8.4 TUI 渲染：单 Panel vs 多 Panel

- **风险**：Rich Live 同 Console 并发是反模式（`scripts/chat_test_agent.py:667-669` 警告）。
- **本期决策**：**不引入并发 Live**（NG2）。子代理事件走"行缓冲，Live 结束后 flush"模式，与现有 `flush_console_buffer`（line 204-246）一致。
- **代价**：TUI 看子代理流式有最多一个主 Agent step 的滞后；用户在 TUI 上要"全流式实时观察后台子代理"建议改用 Web 端。这是已知妥协，文档需明确写给用户。
- **未来**：可在独立 Phase 引入 Rich `Layout` + 自定义 `Console.options` 多区域渲染（非本期范围）。

### 8.5 开放问题（需在实施时确认）

1. **OQ1 — Step 计数策略**：子代理事件 `AgentEvent.step` 字段填什么？方案 A：固定 `parent_step`（本文当前选择，简单，但子代理内部"我跑到第几步了"信息丢失）；方案 B：另开 `payload["subagent_step"]` 字段；方案 C：把 `step` 改成单调全局递增，子代理事件占用真实序号。倾向 A+B（payload 里带 `subagent_step`），实施时验证 UI 端是否需要 B。
2. **OQ2 — 子代理工具调用是否也走 PermissionGate**：今天 `SubagentRunner` 用一个独立的 `tool_registry`（`tools/builtin/task.py:166-189`），它的 `PermissionGate._broker` 是否与父 Agent 共享？如果共享，子代理触发权限请求时父 Agent 的 broker 会收到 `permission.requested` 事件——本期默认共享（向已有事件流复用），但需要在 Step 26 实施时确认 `SubagentRunner` 构造路径里 `permission_gate` 的来源。
3. **OQ3 — VCR 对子代理流的录制**：`docs/design/2026-06-26-llm-streaming-design.md:130-133` 明确 VCR token 级录制是 Phase 5 第 3 项，与本期是平行任务。本期是否同时把子代理的 `stream_raw` 也接入 VCR？倾向：**不接入**，本期只做事件总线，VCR 流式录制另行单独立项；但需要确保 `VCR_ENABLED=true` 时子代理路径不崩（fallback 到 `invoke_raw` 录制，与主 Agent 当前行为一致）。

---

## 9. 工期估算

| Step | 内容 | 估算 |
|------|------|------|
| Step 25 | 事件契约扩展 + TurnExecutor 流式 + SubagentEventBridge + 单元测试 | **0.5 天** |
| Step 26 | 同步 Task 路径接入 + DI 注入 + 集成测试 | **1 天** |
| Step 27 | 后台 Task observer + get_status 派生字段 + TaskOutput 中间态 + 测试 | **1 天** |
| Step 28 | Web 前端 handleEvent + `_STREAM_EVENTS` + TUI renderer + 端到端验收 | **1 天** |
| 缓冲 | 调试 / 回归修复 / 文档同步（更新 `docs/design/2026-06-26-llm-streaming-design.md` 状态行） | **0.5 天** |
| **合计** | | **4 天** |

里程碑：
- Day 1 末：Step 25 合入主干，主 Agent 与 Team Engine 行为零回归。
- Day 2 末：Step 26 合入；同步 `Task` 在 Web 端可见子代理流。
- Day 3 末：Step 27 合入；后台 `Task` + `TaskOutput` 主 Agent 也能感知。
- Day 4 末：Step 28 合入；TUI / Web 渲染齐备，§7 验收场景全通过。
- Day 4.5：文档与 roadmap 收尾。
