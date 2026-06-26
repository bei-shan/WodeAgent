# LLM Streaming 支持设计

> 日期: 2026-06-26 | 状态: **P1 已接入主 ReAct 链路** | 目标: 让用户实时看到 Agent 的回复 token

---

## 一、当前状态

### 已完成

1. `core/llm.py` 已新增 `HelloAgentsLLM.stream_raw()`：
   - 使用 OpenAI-compatible `stream=True`。
   - 默认请求 `stream_options={"include_usage": true}`，若 provider 不支持会自动去掉后重试当前请求。
   - 实时回调 `content` / `reasoning` delta。
   - 累积 content、reasoning、tool_calls、usage、finish_reason，并重建为 `invoke_raw()` 兼容 dict。
2. `agents/codeAgent.py` 的 `_invoke_llm_with_retry()` 已优先走 `stream_raw()`：
   - 后续仍复用 `extract_content()`、`extract_tool_calls()`、`extract_usage()` 等解析器。
   - 若 streaming 失败，会 fallback 到 `invoke_raw()`。
   - 保留空响应 retry、usage 统计、trace 写入。
3. `tui/streaming.py` 的 `StreamingResponse.append()` 已触发 Rich Live update。
4. `scripts/chat_test_agent.py` 已把 `llm_stream_callback` 接入 `StreamingResponse`。
5. 新增配置：`LLM_STREAMING=true|false`，默认开启。

### 仍未完成 / 后续项

1. 当前 CLI 只实时展示 `content`，`reasoning` delta 暂不单独渲染为 thinking 区块。
2. Tool-calling step 已可用流式累积，但工具调用本身仍需等模型完成该 assistant message 后才能执行，这是 function calling 的正常限制。
3. VCR 仍是完整 raw response 级拦截，未记录/replay token events。
4. Team Engine 的 `TurnExecutor` 仍走非流式 `invoke_raw()`。
5. Anthropic/Claude 原生 Messages streaming 还没有 provider-specific adapter。

---

## 二、核心设计

### 2.1 为什么不是直接使用 `think()`

`think()` 只能 yield 文本 token，无法返回完整 raw response，也无法处理 tool_call delta。ReAct 主循环必须在模型消息结束后拿到：

1. `content`
2. `reasoning_content`
3. `tool_calls`
4. `usage`
5. `finish_reason`

因此当前实现采用 `stream_raw(..., on_delta=callback) -> raw_response`。

### 2.2 `stream_raw()` API

```python
raw_response = llm.stream_raw(
    messages,
    tools=tools_schema,
    tool_choice="auto",
    on_delta=lambda event_type, text: ...,
)
```

`on_delta` 事件：

| event_type | 含义 |
|---|---|
| `content` | assistant 正文 token |
| `reasoning` | reasoning / reasoning_content token |

返回值是 dict，结构兼容现有 `core.response_parser`：

```python
{
  "choices": [
    {
      "index": 0,
      "message": {
        "role": "assistant",
        "content": "...",
        "reasoning_content": "...",
        "tool_calls": [...]
      },
      "finish_reason": "stop"
    }
  ],
  "usage": {...}
}
```

### 2.3 ReAct 接入点

`CodeAgent._invoke_llm_with_retry()` 中的真实调用现在是：

```python
if config.llm_streaming and hasattr(llm, "stream_raw"):
    raw_response = llm.stream_raw(..., on_delta=_on_delta)
else:
    raw_response = llm.invoke_raw(...)
```

随后继续走原解析流程，降低对工具调用、trace、history 的影响。

---

## 三、风险与约束

1. **Provider 兼容性**：部分 OpenAI-compatible provider 不支持 `stream_options`，实现已 fallback 去掉该参数。
2. **工具调用实时性**：tool_calls 的 arguments 是 delta 拼接，必须等完整消息结束后才能解析和执行。
3. **重复展示**：CLI 若收到了 streamed content，就不再额外打印最终 response；无 streamed content 时保留原最终打印。
4. **VCR 兼容**：VCR replay 不产生 token events；需要后续扩展 fixture 格式才能精确回放 streaming。

---

## 四、测试状态

已新增 `tests/test_llm_streaming.py`，覆盖：

1. `stream_raw()` 请求参数：`stream=True`、`stream_options`、`tools`、`tool_choice`。
2. content delta 实时回调与最终 content 合并。
3. reasoning delta 实时回调与最终 `reasoning_content` 合并。
4. tool call delta 按 index 合并，并支持 function arguments 分片。
5. usage chunk 合并并可被 `extract_usage()` 解析。
6. provider 不支持 `stream_options` 时去掉参数重试。
7. `think()` / `stream_invoke()` 保持文本 iterator 兼容行为。

验证命令：

```bash
python -m pytest tests/test_llm_streaming.py tests/test_llm_temperature_policy.py tests/test_llm_provider_resolution.py -q
```

## 五、后续路线

1. 把 `reasoning` delta 在 TUI 中渲染为独立 thinking 区块。
2. 为 Team Engine `TurnExecutor` 接入可选 streaming。
3. 扩展 VCR fixture：记录 raw response + stream events。
4. 做 Anthropic Messages API 原生 adapter，支持 Claude content block streaming。
