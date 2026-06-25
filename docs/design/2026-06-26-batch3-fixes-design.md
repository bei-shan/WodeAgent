# 第 3 批修复设计方案

> 日期: 2026-06-26 | 审计编号: P0 #3, P1 #8, P1 #16

---

## 一、P0 #3 — Team Engine work_item 心跳与 watchdog

### 现状

当前 worker 的生命周期管理：

```
worker thread 领取 work_item → status=running → 执行 → status=succeeded/failed
                                                    ↓（如果崩溃）
                                              永久卡在 running ⚠️
```

`requeue_running_work_items()` 只在 `TeamManager.import_state()` 时调用（会话恢复路径）。正常运行中，worker 崩溃后 work_item 永远卡在 `running`。这使得 P0 #3 是"可靠性致命"级别 — Team Engine 在生产环境中不可用。

### 修复方案

**不改变现有架构**（仍是 daemon thread + JSONL 文件），增加两层保护：

#### 第一层：heartbeat_ts 字段

work_item 增加 `heartbeat_ts` 字段，worker 线程每 N 秒更新一次：

```python
# worker.py — 执行循环中
while running:
    work_item = claim_next()
    work_item["heartbeat_ts"] = time.time()
    store.update_work_item(team, work_item)  # 写入心跳
    
    # 执行 LLM turn
    result = execution_service.execute_turn(...)
    
    if done:
        work_item["status"] = "succeeded"
        store.update_work_item(team, work_item)
        break
```

#### 第二层：后台 sweep 线程

TeamManager 启动时 spawn 一个 daemon sweep 线程，每 HEARTBEAT_INTERVAL 秒扫描所有 team 的 work_items：

```python
# manager.py — 新增
HEARTBEAT_INTERVAL = 30  # 心跳间隔（秒）
HEARTBEAT_TIMEOUT = 120  # 超时阈值（秒）— 2 分钟无心跳视为死亡

def _sweep_stale_work_items(self):
    for team_name in self.store.list_teams():
        stale = self.store.find_stale_running_items(team_name, HEARTBEAT_TIMEOUT)
        for item in stale:
            item["status"] = "queued"
            item["started_at"] = None
            item["error"] = f"Worker timed out (no heartbeat for {HEARTBEAT_TIMEOUT}s)"
            self.store.update_work_item(team_name, item)
            self.logger.warning("Requeued stale work_item %s in team %s", item["work_id"], team_name)
```

### 改动清单

| 文件 | 改动 | 行数 |
|------|------|------|
| `core/team_engine/worker.py` | 执行循环中写 heartbeat_ts | +8 |
| `core/team_engine/store.py` | work_item 结构加 heartbeat_ts，新增 `find_stale_running_items()` | +20 |
| `core/team_engine/manager.py` | 启动 sweep 线程，`_sweep_stale_work_items()` | +30 |
| 配置 | `TEAM_HEARTBEAT_TIMEOUT` 环境变量 | — |

### 风险评估

**低**。改动只在 Team Engine 内部，不影响主 ReAct 循环。心跳写入是 append-only JSONL（和现有 append_inbox_message 同模式），sweep 线程独立运行，不阻塞 worker。

---

## 二、P1 #16 — Token 估算替换 chars // 3

### 现状

```python
# history_manager.py:388-409
def estimate_context_tokens(self, pending_input: str) -> int:
    total_chars = len(pending_input or "")
    for msg in self._messages:
        total_chars += len(str(msg.content))
        # ... metadata ...
    return total_chars // 3  # ← 英文 1 token ≈ 4 chars，中文 1 token ≈ 1-1.5 chars
```

对中文，`chars // 3` 严重低估。实际中文 1 token ≈ 1.5 字符，`chars // 3` 给出的估算是真实值的 **2 倍**。这导致压缩触发过晚（真实 60-70K tokens 时才触发"100K 阈值"）。

### 修复方案

**不引入 tiktoken**（它是 OpenAI 专有库，对 DeepSeek/Qwen/Kimi 不适用，且依赖沉重）。

改为 **保守估算**：`chars // 2` 作为默认，加上英文检测自适应：

```python
def estimate_context_tokens(self, pending_input: str) -> int:
    total_chars = len(pending_input or "")
    for msg in self._messages:
        total_chars += len(str(msg.content or ""))
        # ... tool_calls metadata ...
    
    # 保守估算：中文约 1.5 chars/token，英文约 4 chars/token
    # 使用 2.5 作为通用折中（偏保守，宁可早压缩不晚压缩）
    return max(total_chars // 3, total_chars // 2)
```

对于纯中文场景：`total_chars // 2` 提供更准确的估算。对于纯英文场景：`total_chars // 3` 作为下界。

**更精确的方案（v2）**：检测消息中 CJK 字符占比，自适应选择除数：

```python
def _estimate_chars_per_token(self, text: str) -> float:
    cjk = sum(1 for c in text if '一' <= c <= '鿿')
    if len(text) > 0 and cjk / len(text) > 0.3:
        return 2.0   # 中文为主
    return 3.5       # 英文为主
```

### 改动清单

| 文件 | 改动 | 行数 |
|------|------|------|
| `core/context_engine/history_manager.py` | `estimate_context_tokens` 改为保守估算 | +10 |

### 风险评估

**零风险**。这只是压缩触发阈值的估算函数，更保守的估算 → 更早触发压缩 → 更安全。

---

## 三、P1 #8 — CodeAgent 拆分

### 现状

`agents/codeAgent.py` 约 1420 行，职责包括：

| 职责 | 行数 | 位置 |
|------|------|------|
| ReAct 主循环 | ~150 | `_react_loop` |
| LLM 调用 + 重试 | ~100 | `_invoke_llm_with_retry` |
| 工具执行 + 过滤 | ~100 | `_execute_tool`, `_execute_step_tools` |
| 上下文构建 | ~50 | `_build_step_messages` |
| 历史压缩 | ~60 | `_maybe_compress_history` |
| 会话快照 | ~80 | `_build_snapshot`, `save_session`, `resume_session` |
| 斜杠命令处理 | ~120 | `switch_model`, `enter_plan_mode`, `exit_plan_mode`, `enter_worktree`, ... |
| Team 运行时视图 | ~100 | `_format_runtime_system_blocks` |

### 修复方案（最小改动）

**不全拆**（风险太大），而是把最突出的两个职责抽出去：

#### 3a. 抽离 `_format_runtime_system_blocks` → `TeamRuntimeView`

这是审计报告明确指出的问题 — 104 行代码在 CodeAgent 中，但它是 TeamManager 的视图逻辑：

```python
# core/team_engine/runtime_view.py (新建, ~120 行)
class TeamRuntimeView:
    """格式化为 system prompt 注入的团队运行时摘要。"""
    
    @staticmethod
    def format(team_manager, step: int) -> list[str]:
        """返回 system blocks 列表。"""
        ...
```

CodeAgent 改为一行调用：
```python
runtime_blocks.extend(TeamRuntimeView.format(self.team_manager, step))
```

#### 3b. 抽离 SlashCommandHandler → CLI 层

不在 CodeAgent 内部抽，而是在 `chat_test_agent.py` 中把 530 行 if-elif 改成命令注册表：

```python
# scripts/slash_commands.py (新建, ~80 行)
class SlashCommandRegistry:
    _handlers: dict[str, Callable] = {}
    
    def register(self, pattern: str, handler: Callable): ...
    def dispatch(self, user_input: str, agent, console) -> bool: ...
```

### 改动清单

| 文件 | 操作 | 行数 |
|------|------|------|
| `core/team_engine/runtime_view.py` | 新建 | +120 |
| `agents/codeAgent.py` | 删除 `_format_runtime_system_blocks`，改为一行调用 | -100 +5 |
| `scripts/slash_commands.py` | 新建 | +80 |
| `scripts/chat_test_agent.py` | 使用 SlashCommandRegistry | -530 +30 |

### 风险评估

**中等**。`RuntimeView` 的抽取是纯函数提取，行为不变。`SlashCommandRegistry` 影响 CLI 入口，需要逐命令测试。如果 SlashCommandRegistry 风险偏高，可以**只做 3a**，把 3b 推迟。

---

## 四、实施顺序建议

因为 P1 #8 的 CodeAgent 拆分（尤其是 SlashCommandRegistry）风险中等，建议分开提交：

```
Step 1: P0 #3  Team Engine heartbeat  (~60 行, 1 小时)
Step 2: P1 #16 Token 估算修复         (~10 行, 10 分钟)
Step 3: P1 #8a RuntimeView 抽取       (~120 行, 1 小时)
Step 4: P1 #8b SlashCommandRegistry   (~110 行, 1.5 小时, 可选推迟)
```

---

## 五、不做的事情（范围外）

- **不把 CodeAgent 完全拆成多个类** — 风险太大，且 Feature 协议已做好解耦
- **不引入 tiktoken 依赖** — 太重，不适配非 OpenAI provider
- **不改 Team Engine 的 actor 模型** — 当前 daemon thread + JSONL 模式可以工作，heartbeat 修复后可靠性足够
