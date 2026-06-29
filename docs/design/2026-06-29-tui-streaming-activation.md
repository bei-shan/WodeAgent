# P1-D：TUI streaming.py 活性化

**日期**：2026-06-29  
**范围**：`tui/streaming.py` — 从骨架到全功能实时渲染  
**工期**：约 1 天（~6h）  
**状态**：设计中

---

## 一、背景

`tui/streaming.py`（93 行）今天是一个最小可用骨架：它提供了 `StreamingResponse` 类，用 Rich Live 包裹单个 Markdown Panel、蓝边框、blinking 光标（`▌`），并通过 `threading.Lock` 保证线程安全。主 Agent 的 LLM streaming 已在 ReAct 回路中接通（`scripts/chat_test_agent.py:679-681`），但回调仅转发 `content` 事件，**reasoning 被显式丢弃**。所有 `_console()` 输出在 streaming 期间被缓冲（`_console_deferred`），Live 停止后再回放——这是为了避免 Rich Live 与并发的 `console.print()` 互相破坏转义序列。简言之：管道通了，但渲染能力停留在原型级别。

---

## 二、当前缺陷（文件:行）

### 2.1 Reasoning delta 被丢弃

`scripts/chat_test_agent.py:681`：

```python
agent.llm_stream_callback = lambda event_type, text, step: \
    stream.append(text) if event_type == "content" else None
```

LLM 接口 `stream_raw(on_delta)` 同时触发 `"content"` 和 `"reasoning"` 两种事件（见 `core/llm.py`），但回调只消费前者。Reasoning 内容完全丢失——用户看不到模型的思考过程。

### 2.2 无 token 速率显示

`StreamingResponse` 在 `start()` 时记录 `_started_at`（`streaming.py:46`），但 `_render()` 从未计算或展示 `tok/s`。标题、面板内容、footer 中均无速率信息。

### 2.3 标题静态

`chat_test_agent.py:679` 以 `stream.start("Agent")` 启动，标题此后永不更新。ReAct 回路中的 step 索引（`step` 参数在回调中已可用）未被传入渲染层。用户无法从 Live 区域知道当前处于第几步。

### 2.4 扁平布局

`_render()` 返回单个 `Panel(Markdown(...))` （`streaming.py:83-92`）。没有分区：reasoning 与 content 混在一起（如果勉强 append 的话），没有 step 状态栏，没有 footer 指标。

### 2.5 无多通道输出 API

`StreamingResponse` 只有 `append(chunk)` 一个入口和一个从未被使用的 `append_text()`。没有 `append_reasoning()`、`update_step_label()`、`update_title()` 等方法。这意味着 reasoning token 要么被丢弃，要么强行和 content 混排——两种都不可接受。

### 2.6 设计文档确认此为有意识延后

- `docs/design/2026-06-26-llm-streaming-design.md` 第 130 行将 "reasoning delta 在 TUI 中渲染为独立 thinking 区块" 列为后续路线第 1 项。
- `docs/design/2026-06-29-subagent-streaming-design.md` 第 92 行将 "不在本期处理 reasoning 流" 标记为 **NG5**，并明确"TUI 是否渲染 thinking 块属于另一独立 Phase"。

---

## 三、设计：活性化后的渲染器

### 3.1 目标形态

`StreamingResponse` 从"单个 Rich Live Panel"演进为"结构化多区域 token 实时视图"：

```
┌─── Step 2 · Thinking ─────────────────────── 1.2k tok/s ───┐
│                                                             │
│  ┌─ Reasoning ───────────────────────────────────────────┐  │
│  │ 我们需要先读取目标文件的内容，然后分析其结构…           │  │
│  │ …（模型内部推理，可折叠）▌                             │  │
│  └────────────────────────────────────────────────────────┘  │
│                                                             │
│  ┌─ Response ────────────────────────────────────────────┐  │
│  │ 我来帮你分析这个文件的结构…                            │  │
│  │                                                        │  │
│  │ ```python                                              │  │
│  │ def main():                                            │  │
│  │     ...                                                │  │
│  │ ```▌                                                   │  │
│  └────────────────────────────────────────────────────────┘  │
│                                                             │
│  reasoning: 847 tokens  │  response: 1,203 tokens           │
└─────────────────────────────────────────────────────────────┘
```

在一个 `Live` 实例内，`_render()` 返回一个 `Group`，包含两个 `Panel`（reasoning / response），用 `Table` 或 `Columns` 纵排。顶部标题栏显示 step 标签 + 速率，底部 footer 显示分段 token 计数。

### 3.2 API 设计

```python
class StreamingResponse:
    def start(self, title: str = "") -> None: ...
    def append(self, chunk: str) -> None: ...
    def append_reasoning(self, chunk: str) -> None: ...
    def update_title(self, title: str) -> None: ...
    def finish(self) -> tuple[str, str]: ...
    # 返回 (content_text, reasoning_text)

    # 内部新增
    _reasoning_buffer: list[str]
    _token_count: int
    _step_label: str
```

### 3.3 关键设计决策

**不引入多 Live / 嵌套 Live**。Rich Live 与同 Console 的其他 `console.print()` 并发是已知反模式（subagent streaming 设计 doc NG2 已确认）。活性化仍在**单个 `Live` 实例**内完成，通过 `Group` 或 `Layout` 类实现结构化分区，不触及 Console 全局状态。

**Reasoning 默认折叠或显示为独立 Panel**。非 streaming 模式中 `ChatTestAgent._console()` 已用 `Panel(..., title="Reasoning", border_style="magenta")` 渲染 reasoning（`chat_test_agent.py:217-220`）。活性化后 streaming 模式与此视觉一致：magenta 边框、小字、默认展开但置于 content 上方。

**Buffering 机制不变**。`_console_deferred` 与 `_streaming_mode` 的协作逻辑保持不动。Live 运行时 step 标记、tool 调用仍走 buffer；Live 停止后 `flush_console_buffer()` 回放。

---

## 四、实现步骤

### Step 1：StreamingResponse 内部重构（~2h）

**文件**：`tui/streaming.py`

1. 新增 `_reasoning_buffer: list[str]`、`_token_count: int`、`_step_label: str` 属性。
2. 新增 `append_reasoning(chunk)` —— 写入 `_reasoning_buffer` 并触发 `_live.update()`。与 `append()` 共享同一把锁。
3. 新增 `update_title(title)` —— 更新 `_title` 并触发刷新。调用方可传入 `"Step 2 · Thinking"` 之类的动态标签。
4. 修改 `_render()`：
   - 按 reasoning 是否有内容，动态构建一个或两个 `Panel`。
   - Reasoning panel：`border_style="magenta"`、`title="Reasoning"`。
   - Content panel：`border_style="blue"`、`title="Response"`（或在有 title 时沿用现有标题）。
   - 两个 panel 用 `Group` 纵排。
   - 顶部或底部用 `Text` 显示 `tok/s`（从 `_started_at` 和 `_token_count` 实时计算）。
   - 保留 blinking 光标在 content 末尾。
5. 修改 `finish()` 返回 `tuple[str, str]`：`(content_text, reasoning_text)`。

**不涉及** `chat_test_agent.py` 改动。此步完成后 `StreamingResponse` 具备全 API 但尚未被调用方使用。

### Step 2：接线——回调分流 + step 标签（~1.5h）

**文件**：`scripts/chat_test_agent.py`（约 670-700 行区域）

1. 修改 `llm_stream_callback`（line 681）：

```python
def _on_stream_delta(event_type: str, text: str, step: int) -> None:
    if event_type == "content":
        stream.append(text)
    elif event_type == "reasoning":
        stream.append_reasoning(text)

agent.llm_stream_callback = _on_stream_delta
```

2. 在 ReAct 回路中（`agents/codeAgent.py` 或 `chat_test_agent.py` 的 step 推进点），step 变化时调用 `stream.update_title(f"Step {n} · Thinking")`。切入点可选：
   - `core/events.py` 中 `EventType.STEP_STARTED` 已存在——在 `chat_test_agent.py` 中监听该事件并触发 `update_title`。
   - 或直接扩展 `llm_stream_callback` 的使用方式：在每次 `agent.run()` 内 step 推进时检查 step 变化并更新标题。

3. 修改 `stream.finish()` 调用点（line 687）以接收 `(content, reasoning)` 二元组，并在 `flush_console_buffer()` 之前将 reasoning 内容打印到 console（非 streaming 模式下的 reasoning 渲染发生在 `flush_console_buffer` 的 `tag == "reasoning"` 分支，streaming 模式 reasoning 已在 Live 中实时展示，回放时跳过以避免重复）。

4. **Buffer 冲突处理**：当 reasoning 已实时渲染后，`_console_deferred` 中的 `"reasoning"` tag 条目在 `flush_console_buffer()` 时需要跳过。在 `ChatTestAgent` 上添加一个 `_streamed_reasoning` 标记或在 `flush_console_buffer` 中检查 reasoning buffer 是否已非空。

### Step 3：测试（~2h）

**文件**：`tests/test_streaming.py`（新建）或扩展现有 `tests/test_task_tool.py`

1. **单元测试**：
   - `test_append_reasoning`：验证 `append_reasoning` 写入 `_reasoning_buffer` 且不污染 `_buffer`。
   - `test_finish_returns_both`：验证 `finish()` 返回 `(content_text, reasoning_text)` 二元组。
   - `test_update_title`：验证 `update_title()` 更新 `_render()` 输出中的标题。
   - `test_token_rate_display`：验证 `_render()` 在 token 数 > 0 且时间 > 0 时包含 `tok/s` 文本。

2. **集成测试**：
   - `test_reasoning_stream_flow`：模拟 `stream_raw` 触发 `"reasoning"` delta → 验证 `append_reasoning` 被调用。
   - `test_callback_forwards_reasoning`：验证 `chat_test_agent.py` 中的回调在 `event_type == "reasoning"` 时调用 `append_reasoning` 而非丢弃。

3. **回归**：现有 streaming 相关测试通过（`test_llm_streaming.py` 等），`ChatTestAgent` 的非 streaming 路径 reasoning 渲染不变。

---

## 五、验收标准

活性化完成后，用户在 TUI 中看到一个完整的 ReAct 步骤时：

1. **Streaming 期间**：
   - Live 区域顶部显示 `"Step 2 · Thinking"`，并在进入 tool-calling 阶段时动态变化（如 `"Step 2 · Using Tool"`）。
   - 若模型产生 reasoning token，magenta 边框的 Reasoning 面板**实时出现并逐 token 增长**，与下方的蓝色 Response 面板**同时可见**。
   - 标题栏或 footer 显示实时 `tok/s`（例如 `"1.2k tok/s"`），随 streaming 进行而更新。
   - Response 面板末尾保留 blinking 光标 `▌`。

2. **Streaming 结束后**：
   - `flush_console_buffer()` 回放 step 标记行、thought、action、observation 等，按现有顺序渲染。
   - Reasoning 内容**不再重复打印**（已在 Live 中实时展示过）。
   - `finish()` 返回的 content 文本被 `_print_assistant_response()` 正常输出。

3. **非 streaming 模式**（`LLM_STREAMING=false` 或 `--no-stream`）：
   - 行为**完全不变**。Reasoning 仍通过 `_console()` → `flush_console_buffer()` 的 `tag == "reasoning"` 分支渲染为独立的 magenta Panel。
   - 所有现有测试通过。

---

## 六、风险与边界

| 风险 | 缓解 |
|------|------|
| Rich Live 内嵌多 Panel 的刷新性能 | 单 Live + `refresh_per_second=10` 不变；Group 纵排的开销与单 Panel 同级 |
| reasoning 极少出现（取决于模型） | API 设计独立但 fallback：reasoning buffer 为空时 `_render()` 只显示 content panel，布局与现状一致 |
| `finish()` 返回值从 `str` 变为 `tuple[str, str]` 破坏调用方 | Step 2 中一并修改 `chat_test_agent.py:687`；全局搜索 `stream.finish()` 确认无其他调用方 |
| buffer 重复渲染（reasoning 既在 Live 中展示又被 `flush_console_buffer` 回放） | Step 2.4 中通过标记位去重 |

## 七、不在此范围

- **不做 Rich Layout 多面板并行**：仍在单 Live 内渲染，不做 `Layout.split_column()` / 多 Console 实例（与 subagent streaming 设计 NG2 对齐）。
- **不做 reasoning 可折叠交互**：Reasoning panel 始终展开。折叠/展开需要 key-binding 集成，超出本 Phase 范围。
- **不做子代理 streaming 的 TUI 渲染**：仅主 Agent（`chat_test_agent.py` 路径）。子代理 streaming 在 `2026-06-29-subagent-streaming-design.md` 中独立处理。
- **不改变 `_console_deferred` 缓冲机制**：只在其消费端（`flush_console_buffer`）增加去重逻辑。
