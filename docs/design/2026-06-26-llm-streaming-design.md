# LLM Streaming 支持设计

> 日期: 2026-06-26 | 目标: 让用户实时看到 Agent 的思考和回复

---

## 一、问题分析

### 1.1 当前状态

```
用户输入 → Agent 开始处理 → 用户看到 "⏳ Agent 正在处理..." 
  → 10-30s 空白等待
  → 一次性显示完整回复
```

`HelloAgentsLLM.invoke_raw()` 使用 `stream=False`，同步阻塞等完整响应。

### 1.2 为什么看似"已经支持"却不工作

`core/llm.py` 有 `think()` 方法（流式 yield token），`tui/streaming.py` 有 `StreamingResponse`（Rich Live 渲染）。但 ReAct 循环走的是 `invoke_raw()` → 必须等完整响应才能解析 `tool_calls`。

### 1.3 关键洞察：DeepSeek 的 reasoning_content 可以先流

DeepSeek V4 的 API 在流式模式下会先推送 `reasoning_content`（思考过程），再推送最终 `content`。这就是 Pi Agent 的 "thinking" 区块的来源。

---

## 二、设计方案

### 2.1 核心思路：流式调用 + 累积解析

```python
# 核心改动：HelloAgentsLLM 新增 stream_raw()
def stream_raw(self, messages, tools=None, tool_choice=None):
    """流式调用 LLM，实时推送 reasoning + content 到回调，返回完整响应对象。"""
    chunks = []
    reasoning_chunks = []
    
    response = self._client.chat.completions.create(
        model=self.model,
        messages=messages,
        tools=tools,
        tool_choice=tool_choice,
        stream=True,
        stream_options={"include_usage": True},
    )
    
    for chunk in response:
        chunks.append(chunk)
        delta = chunk.choices[0].delta if chunk.choices else None
        if delta:
            if delta.reasoning_content:
                reasoning_chunks.append(delta.reasoning_content)
                yield ("reasoning", delta.reasoning_content)
            elif delta.content:
                yield ("content", delta.content)
    
    # 累积后重建完整响应（与 invoke_raw 返回格式一致）
    return self._merge_streaming_chunks(chunks, reasoning_chunks)
```

### 2.2 ReAct 循环改动

```python
# 之前：
raw_response = self.llm.invoke_raw(messages, tools=..., tool_choice=...)

# 之后：
stream_gen = self.llm.stream_raw(messages, tools=..., tool_choice=...)
for event_type, text in stream_gen:
    if event_type == "reasoning":
        self._console(f"🧠 {text}")  # 实时推送到 TUI
    elif event_type == "content":
        self._console(text)           # 实时推送
raw_response = stream_gen.return_value  # 完整响应对象
```

### 2.3 `_merge_streaming_chunks` 实现

OpenAI SDK 的流式 chunk 可以通过累积来重建完整响应：

```python
def _merge_streaming_chunks(self, chunks, reasoning_chunks):
    """将流式 chunk 列表合并为与 invoke_raw 返回格式一致的对象。"""
    # 使用 SDK 内置的累积逻辑
    from openai import Stream
    # 简单方案：手动构建兼容对象
    class MergedResponse:
        choices = [type('Choice', (), {
            'index': 0,
            'message': type('Message', (), {
                'role': 'assistant',
                'content': self._extract_text_from_chunks(chunks),
                'tool_calls': self._extract_tool_calls_from_chunks(chunks),
                'reasoning_content': ''.join(reasoning_chunks) if reasoning_chunks else None,
            })(),
            'finish_reason': self._extract_finish_reason(chunks),
        })()]
        usage = self._extract_usage_from_chunks(chunks)
    return MergedResponse()
```

---

## 三、文件清单

| 文件 | 操作 | 行数 |
|------|------|------|
| `core/llm.py` | 修改：新增 `stream_raw()` + `_merge_streaming_chunks()` | +80 |
| `agents/codeAgent.py` | 修改：`_invoke_llm_with_retry` 改用 `stream_raw` | +30 |
| `tui/streaming.py` | 修改：已有 `StreamingResponse`，微调 | +10 |

## 四、预期效果

```
之前：
  ⏳ Agent 正在处理... (15s)
  ────────────────────
  ✅ Agent 已完成
  (一次性显示完整回复)

之后：
  ⏳ Agent 正在处理...
  🧠 用户想要的是...应该先检查... ← reasoning 实时推送
  📝 我来帮你写这个 API...      ← content 实时推送
  ✅ Agent 已完成 (3s)
```

## 五、风险评估

- **低风险**：不改变 API 协议，`stream_raw` 返回格式与 `invoke_raw` 兼容
- **重试兼容**：流式调用不重试（重试只对非流式有意义）
- **工具调用兼容**：累积 chunk 后解析 tool_calls 与现有逻辑一致
- **DeepSeek 兼容**：已验证 DeepSeek API 支持 `stream=True` + `stream_options`
