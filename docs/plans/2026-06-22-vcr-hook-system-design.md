# VCR & Hook System 功能设计文档

> 日期: 2026-06-22 | 优先级: P3 | 来源: Kode-Agent 学习计划
> 范围: VCR (LLM API 录制回放) + Hook (生命周期钩子)

---

## 一、功能概述

### 1.1 VCR — LLM API 录制回放

VCR（Video Cassette Recorder 隐喻）是一个 LLM API 调用的录制回放系统。将真实 API 的请求/响应对保存为 fixture 文件，后续测试时直接回放 fixture，不再调用真实 API。

**效果：** 测试零成本、秒级完成、100% 确定性。

### 1.2 Hook System — 生命周期钩子

Hook 允许用户在 Agent 的关键生命周期节点插入自定义脚本，实现工作流自动化。例如：提交前自动 lint、Bash 命令执行前审计、会话启动时注入环境变量。

---

## 二、Kode-Agent 参考分析

### 2.1 VCR

| 维度 | Kode-Agent 做法 |
|------|----------------|
| 范围 | 仅 LLM 调用（`queryLLM` 一层） |
| 触发 | `NODE_ENV === 'test'` 时自动启用 |
| 录制 | 本地缺 fixture → 调 API → 写入 `./fixtures/<sha1>.json` |
| 回放 | fixture 存在 → 直接返回，不调 API |
| CI | fixture 不存在 → 调 API 但不写入 |
| 格式 | JSON `{input: [...], output: {...}}` |
| 去重 | 对 messages 做 `dehydrate`（去时间戳/CWD），再 SHA-1 |
| 代码量 | 159 行 (`src/services/system/vcr.ts`) |

**dehydrate 细节：**
- `num_files="\d+"` → `num_files="[NUM]"`
- `duration_ms="\d+"` → `duration_ms="[DURATION]"`
- `cost_usd="\d+"` → `cost_usd="[COST]"`
- CWD → `[CWD]`
- `Files modified by user:` 行 → 固定字符串

### 2.2 Hook System

| 维度 | Kode-Agent 做法 |
|------|----------------|
| 事件 | PreToolUse, PostToolUse, Stop, SubagentStop, UserPromptSubmit, SessionStart, SessionEnd |
| 类型 | `command` (Shell 子进程) + `prompt` (LLM 调用, Kode 独有) |
| 配置 | `.kode/settings.json` + 插件 `hooks.json` |
| 匹配 | 工具名 / glob / 正则 / `*` |
| 执行 | 并发 (`Promise.allSettled`) |
| 退出码 | 0=成功, 1=警告, 2=硬阻止 |
| 输入 | stdin JSON (command) / 系统提示 (prompt) |
| 输出 | stdout JSON (command) / LLM JSON 响应 (prompt) |
| 能力 | 阻止/修改工具调用、注入系统消息、覆盖权限决策 |
| 代码量 | 1845 行 (`src/utils/session/kodeHooks.ts`) |

---

## 三、MyCodeAgent 实现设计 — VCR

### 3.1 架构

```
VCR 拦截层 (core/vcr.py)
    ↓ 包裹
HelloAgentsLLM.invoke_raw()   ← 唯一的 LLM API 调用入口
    ↓
VCR 判断:
    ├── VCR_ENABLED != true → 透传，不干预
    ├── fixture 存在 → 从文件回放
    ├── fixture 不存在 + VCR_RECORD_MODE=new_episodes → 调 API + 写入
    ├── fixture 不存在 + VCR_RECORD_MODE=once → 调 API + 写入
    └── fixture 不存在 + VCR_RECORD_MODE=none → 抛异常
```

### 3.2 配置

```bash
# .env
VCR_ENABLED=true                    # 启用 VCR（测试环境建议开启）
VCR_RECORD_MODE=new_episodes        # new_episodes | once | none
VCR_FIXTURE_DIR=tests/fixtures/vcr  # fixture 存储目录
```

| 模式 | fixture 存在时 | fixture 不存在时 |
|------|--------------|----------------|
| `new_episodes` (默认) | 回放 | 调 API + 写入新 fixture |
| `once` | 回放 | 调 API + 写入新 fixture |
| `none` | 回放 | 抛出 `VCRFixtureMissing` 异常 |

### 3.3 Fixture 格式

```json
{
  "version": 1,
  "created_at": "2026-06-22T10:00:00",
  "input": {
    "model": "deepseek-v4-pro",
    "messages_hash": "a1b2c3d4e5f6...",
    "messages_summary": "system(2) + user: 'Write a function that...'",
    "tools_hash": "f6e5d4c3b2a1...",
    "tool_choice": "auto"
  },
  "output": {
    "id": "chatcmpl-mock-0001",
    "choices": [{
      "index": 0,
      "message": {
        "role": "assistant",
        "content": "...",
        "tool_calls": null
      },
      "finish_reason": "stop"
    }],
    "usage": {
      "prompt_tokens": 0,
      "completion_tokens": 0,
      "total_tokens": 0
    }
  }
}
```

### 3.4 Dehydrate 策略

对 messages 做标准化处理后再哈希，确保跨环境一致性：

```python
def _dehydrate_message(msg: dict) -> dict:
    """去除消息中的非确定性内容。"""
    dehydrated = dict(msg)
    content = str(dehydrated.get("content", ""))
    # 替换 CWD 路径
    content = content.replace(cwd, "[CWD]")
    # 替换临时路径
    content = re.sub(r'/tmp/[a-zA-Z0-9_/-]+', '[TMP]', content)
    # 替换 Windows 临时路径
    content = re.sub(r'C:\\Users\\[^\\]+\\AppData\\Local\\Temp\\[a-zA-Z0-9_\\-]+', '[TMP]', content)
    dehydrated["content"] = content
    return dehydrated
```

### 3.5 文件名策略

```python
# SHA-256 of (model + JSON(messages) + JSON(tools_schema))
fingerprint = hashlib.sha256(
    f"{model}|{json.dumps(messages, sort_keys=True)}|{json.dumps(tools_schema or {}, sort_keys=True)}"
    .encode()
).hexdigest()[:16]

fixture_path = f"{VCR_FIXTURE_DIR}/{fingerprint}.json"
```

### 3.6 集成方式

**不修改 `HelloAgentsLLM` 类本身**（保持 LLM 客户端纯净）。在 `CodeAgent._invoke_llm_with_retry()` 中包裹：

```python
def _invoke_llm_with_retry(self, messages, ...):
    if self._vcr:
        raw_response = self._vcr.call(
            model=self.llm.model,
            messages=messages,
            tools=tools_schema,
            fallback=lambda: self.llm.invoke_raw(messages, tools=tools_schema, ...)
        )
    else:
        raw_response = self.llm.invoke_raw(messages, tools=tools_schema, ...)
```

### 3.7 文件清单

| 文件 | 操作 | 说明 |
|------|------|------|
| `core/vcr.py` | **新建** | VCR 录制回放引擎 |
| `agents/codeAgent.py` | **修改** | 集成 VCR 拦截 (+15 行) |
| `.env.example` | **修改** | VCR 配置项 |
| `tests/test_vcr.py` | **新建** | VCR 测试 |

### 3.8 预估

| 文件 | 行数 |
|------|------|
| `core/vcr.py` | ~180 |
| `codeAgent.py` (修改) | +15 |
| `tests/test_vcr.py` | ~150 |
| **总计** | **~345** |

---

## 四、MyCodeAgent 实现设计 — Hook System

### 4.1 架构

```
配置层: .mycode/hooks.json (项目根目录)
    ↓
加载层: core/hook_system.py
    ├── HookManager
    │   ├── _load_config()         → 解析 .mycode/hooks.json
    │   ├── _match(event, tool)    → 匹配 matcher
    │   ├── _execute_command()     → subprocess 执行
    │   └── _parse_output()        → JSON stdout 解析
    └── HookMatcher (dataclass)
        ├── matcher: str           # "*" | "Bash" | "Write" | glob
        ├── hooks: list[Hook]
        └── Hook (dataclass)
            ├── type: "command"
            ├── command: str       # "python scripts/audit.py"
            └── timeout: int       # 默认 30s

注入层: agents/codeAgent.py
    ├── __init__()         → run_session_start_hooks()
    ├── _execute_tool()    → run_pre_tool_use() / run_post_tool_use()
    └── close()            → run_session_end_hooks()
```

### 4.2 支持的事件 (v1)

| 事件 | 触发时机 | 能力 |
|------|---------|------|
| `SessionStart` | Agent 初始化完成后 | 注入系统消息、设置环境变量 |
| `PreToolUse` | 工具调用前 | 阻止/修改工具调用 |
| `PostToolUse` | 工具调用后 | 注入系统消息、记录审计日志 |
| `SessionEnd` | Agent 关闭时 | 清理资源、写入报告 |

**v2 候选事件：** Stop, UserPromptSubmit, SubagentStop

### 4.3 配置格式 (`.mycode/hooks.json`)

```json
{
  "hooks": {
    "SessionStart": [
      {
        "matcher": "*",
        "hooks": [
          {
            "type": "command",
            "command": "python .mycode/hooks/load_env.py",
            "timeout": 10
          }
        ]
      }
    ],
    "PreToolUse": [
      {
        "matcher": "Bash",
        "hooks": [
          {
            "type": "command",
            "command": "python .mycode/hooks/bash_audit.py",
            "timeout": 30
          }
        ]
      },
      {
        "matcher": "Write",
        "hooks": [
          {
            "type": "command",
            "command": "python .mycode/hooks/pre_write_check.py",
            "timeout": 15
          }
        ]
      }
    ],
    "PostToolUse": [
      {
        "matcher": "*",
        "hooks": [
          {
            "type": "command",
            "command": "python .mycode/hooks/audit_log.py",
            "timeout": 10
          }
        ]
      }
    ],
    "SessionEnd": [
      {
        "matcher": "*",
        "hooks": [
          {
            "type": "command",
            "command": "python .mycode/hooks/session_report.py",
            "timeout": 15
          }
        ]
      }
    ]
  }
}
```

### 4.4 Matcher 规则

| Matcher | 匹配 |
|---------|------|
| `"*"` | 所有工具 / 所有事件 |
| `"Bash"` | 精确匹配工具名 |
| `"Write"` | 精确匹配工具名 |
| `"Team*"` | glob 匹配 (TeamCreate, TeamFanout, ...) |
| `"/regex/"` | 正则匹配 (暂不实现，v2) |

### 4.5 Hook 执行协议

**输入 (stdin JSON):**
```json
{
  "hook_event_name": "PreToolUse",
  "session_id": "abc123",
  "project_root": "/path/to/project",
  "cwd": "/path/to/project",
  "tool_name": "Bash",
  "tool_input": {
    "command": "rm -rf /",
    "description": "delete everything"
  }
}
```

**输出 (stdout JSON):**
```json
{
  "decision": "block",
  "reason": "Dangerous command detected: rm -rf /",
  "system_message": "The Bash tool call was blocked by the security hook.",
  "updated_input": {
    "command": "echo 'blocked'"
  }
}
```

**退出码约定：**

| 退出码 | 含义 | 行为 |
|--------|------|------|
| 0 | 成功 | 解析 stdout JSON，应用 decision/updated_input/system_message |
| 1 | 警告 | stderr 作为 warning 日志，不阻止操作 |
| 2 | 硬阻止 | 阻止操作（PreToolUse: 拒绝工具调用; SessionStart: 记录警告） |

### 4.6 输出字段说明

| 字段 | 适用事件 | 说明 |
|------|---------|------|
| `decision` | PreToolUse | `"approve"` / `"block"` |
| `reason` | PreToolUse, SessionEnd | 阻止原因或结束原因 |
| `system_message` | 全部 | 注入到下一次 LLM 调用的系统消息 |
| `updated_input` | PreToolUse | 修改后的工具参数（浅合并到原始 input） |
| `additional_context` | SessionStart | 注入到系统提示的额外上下文 |

### 4.7 并发执行

多个匹配的 hook 并发执行（`concurrent.futures.ThreadPoolExecutor`），任一返回 `block` 则立即阻止。`system_message` 和 `additional_context` 合并所有 hook 的输出。

### 4.8 超时处理

每个 hook 有独立超时（默认 30 秒，可通过 `timeout` 字段配置）。超时的 hook 视为失败，记录警告但不阻止操作。

### 4.9 环境变量注入 (SessionStart)

`SessionStart` hook 可以通过输出中的 `additional_context` 注入系统提示内容。同时，hook 进程可以写入 `MYCODE_ENV_FILE` 指向的文件来设置环境变量：

```
SessionStart hook 进程环境变量:
  MYCODE_PROJECT_DIR=/path/to/project
  MYCODE_ENV_FILE=/tmp/mycode-env-abc123/env

hook 脚本写入 MYCODE_ENV_FILE:
  export MY_CUSTOM_VAR=value
  export ANOTHER_VAR=123

→ Agent 主进程读取并 merge 到 os.environ
```

### 4.10 集成点

```python
# agents/codeAgent.py

def __init__(self, ...):
    # ... existing init ...
    self._hook_manager = HookManager(project_root=self._original_project_root)
    self._hook_manager.run_session_start(self)

def _execute_tool(self, tool_name, tool_input):
    # PreToolUse
    pre_result = self._hook_manager.run_pre_tool_use(tool_name, tool_input)
    if pre_result.blocked:
        return json.dumps({"status": "error", "error": {"code": "HOOK_BLOCKED", "message": pre_result.reason}})
    if pre_result.updated_input:
        tool_input = {**tool_input, **pre_result.updated_input}
    if pre_result.system_messages:
        self._queue_hook_messages(pre_result.system_messages)

    result = super()._execute_tool(tool_name, tool_input)

    # PostToolUse
    post_result = self._hook_manager.run_post_tool_use(tool_name, tool_input, result)
    if post_result.system_messages:
        self._queue_hook_messages(post_result.system_messages)

    return result

def close(self):
    self._hook_manager.run_session_end()
    # ... existing close logic ...
```

### 4.11 文件清单

| 文件 | 操作 | 说明 |
|------|------|------|
| `core/hook_system.py` | **新建** | Hook 引擎 (~400 行) |
| `agents/codeAgent.py` | **修改** | 集成 Hook 调用点 (+50 行) |
| `.env.example` | **修改** | Hook 配置项 |
| `tests/test_hook_system.py` | **新建** | Hook 测试 |

### 4.12 预估

| 文件 | 行数 |
|------|------|
| `core/hook_system.py` | ~400 |
| `codeAgent.py` (修改) | +50 |
| `tests/test_hook_system.py` | ~250 |
| **总计** | **~700** |

---

## 五、VCR + Hook 总汇

| 项目 | 文件数 | 预估行数 | 难度 |
|------|--------|---------|------|
| VCR | 3 (+1 修改) | ~345 | 低 |
| Hook | 3 (+1 修改) | ~700 | 中 |
| **合计** | **6 (+2 修改)** | **~1045** | — |

---

## 六、实施步骤

```
Phase 1: VCR
  Step 1: 创建 core/vcr.py (VCR 引擎)
  Step 2: 修改 agents/codeAgent.py (集成 VCR)
  Step 3: 更新 .env.example
  Step 4: 编写测试 test_vcr.py
  Step 5: 验证全量测试

Phase 2: Hook System
  Step 6: 创建 core/hook_system.py (Hook 引擎)
  Step 7: 修改 agents/codeAgent.py (集成 Hook 调用点)
  Step 8: 更新 .env.example
  Step 9: 编写测试 test_hook_system.py
  Step 10: 验证全量测试
```

---

## 七、不做的事情 (v1 范围外)

- **Prompt 类型 hook** (LLM 执行的 hook) — Kode 独有，增加复杂性
- **Stop / UserPromptSubmit / SubagentStop 事件** — v2
- **正则 matcher** — glob 已覆盖主要场景
- **插件系统 hook 发现** — MyCodeAgent 无插件体系
- **`updated_input` 重新验证** (zod schema validate) — Python 无原生 schema，v2
- **hook transcript 文件** — v2
