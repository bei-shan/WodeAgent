# Token 用量预算设计文档

> 日期: 2026-06-22 | 优先级: P2 | 参考: Claude Code budget 指令

---

## 一、功能概述

类似 Claude Code 的 `+500k` 预算指令，Agent 在系统提示中被告知剩余的 token 预算，并在接近耗尽时自动提醒用户。这防止了长对话中不知不觉消耗大量 token。

---

## 二、Claude Code 参考

```
用户: "帮我重构 auth 模块 +500k"
  → 系统注入: "Budget: 500000 tokens remaining this conversation."
  → 每轮 LLM 调用后扣减 usage.total_tokens
  → 剩余 < 20% 时: "⚠️ Budget: 45000/500000 tokens remaining (9%)"
  → 耗尽: "Budget exceeded. Conversation will be compacted."
```

Kode-Agent 中 `budget` 对象暴露给 Workflow 脚本：
```typescript
budget.total       // 500000 | null
budget.spent()     // 当前已消耗
budget.remaining() // max(0, total - spent)
```

---

## 三、MyCodeAgent 实现

### 3.1 预算设置

用户通过系统提示中的指令设置预算：

```
用户: "帮我优化所有文件，预算 10 万 token"
  → Agent 解析出 budget=100000
  → 注入 runtime block: "Budget: 100000 tokens remaining. 100000 spent."

用户: "重构 auth 模块 +500k"
  → Agent 解析出 budget=500000
```

支持格式：
- `+500k` / `+50万` → 500000
- `预算 10 万` → 100000
- `budget 200k` → 200000
- 不指定 → 无预算限制

### 3.2 预算追踪

```python
# core/budget_tracker.py (新建)
class BudgetTracker:
    def __init__(self, total: int | None = None):
        self.total = total          # None = unlimited
        self._spent: int = 0

    @property
    def remaining(self) -> int | None:
        if self.total is None:
            return None
        return max(0, self.total - self._spent)

    def spend(self, tokens: int) -> None:
        self._spent += tokens

    def is_exceeded(self) -> bool:
        if self.total is None:
            return False
        return self._spent >= self.total

    def warning_level(self) -> str:
        """Returns 'ok', 'warn', or 'critical'."""
        if self.total is None:
            return "ok"
        pct = self._spent / self.total
        if pct >= 1.0:
            return "critical"
        if pct >= 0.8:
            return "warn"
        return "ok"

    def status_text(self) -> str:
        """Human-readable status for runtime block."""
        if self.total is None:
            return ""
        pct = (self._spent / self.total) * 100
        if self.is_exceeded():
            return (
                f"## Budget Exceeded\n"
                f"Token budget of {self.total:,} has been exceeded "
                f"({self._spent:,} spent). "
                f"The conversation may be compacted soon."
            )
        return (
            f"## Token Budget\n"
            f"Budget: {self.total:,} tokens total. "
            f"{self.remaining:,} remaining ({pct:.0f}% used). "
            f"Plan your remaining work accordingly."
        )
```

### 3.3 集成到 ReAct 循环

```python
# agents/codeAgent.py
class CodeAgent:
    _budget_tracker: BudgetTracker

    def __init__(self, ...):
        self._budget_tracker = BudgetTracker()

    def _react_loop(self, ...):
        for step in range(1, self.max_steps + 1):
            # 注入预算状态
            status = self._budget_tracker.status_text()
            if status:
                runtime_blocks.append(status)

            # ... LLM 调用 ...
            result = self._invoke_llm_with_retry(...)

            # 扣减 token
            usage = extract_usage(raw_response)
            if usage and usage.get("total_tokens"):
                self._budget_tracker.spend(usage["total_tokens"])

            # 预算耗尽处理
            if self._budget_tracker.is_exceeded():
                self._console("⚠️ Token budget exceeded. Compacting conversation...")
                self._maybe_force_compact()
```

### 3.4 预算解析

```python
def parse_budget_from_input(user_input: str) -> int | None:
    """Extract token budget from user input.

    Supports:
    - "+500k" → 500000
    - "+50万" → 500000
    - "预算 10 万" → 100000
    - "budget 200k" → 200000
    - "+1m" → 1000000
    """
    import re

    # +500k / +1m
    m = re.search(r'\+(\d+)\s*([km])', user_input, re.IGNORECASE)
    if m:
        num = int(m.group(1))
        unit = m.group(2).lower()
        return num * 1000 if unit == 'k' else num * 1000000

    # +50万
    m = re.search(r'\+(\d+)\s*万', user_input)
    if m:
        return int(m.group(1)) * 10000

    # 预算 10 万 / budget 200k
    m = re.search(r'(?:预算|budget)\s*(\d+)\s*(万|[kKmM]?)', user_input, re.IGNORECASE)
    if m:
        num = int(m.group(1))
        unit = (m.group(2) or '').lower()
        if unit == '万':
            return num * 10000
        elif unit == 'k':
            return num * 1000
        elif unit == 'm':
            return num * 1000000
        return num

    return None
```

### 3.5 预算耗尽行为

```
剩余 > 20% → 正常执行，每轮显示预算状态
剩余 < 20% → 显示 ⚠️ 警告
剩余 = 0   → 1. 自动触发上下文压缩
             2. 提示用户预算已耗尽
             3. 如果压缩后仍不够，停止执行
```

---

## 四、CLI 集成

```bash
# 启动时设置默认预算
python scripts/chat_test_agent.py --budget 200000
```

**Slash command:**
```
/budget          → 显示当前预算状态
/budget 500k     → 设置预算
/budget none     → 取消预算限制
```

---

## 五、实现

### 文件清单

| 文件 | 操作 | 说明 |
|------|------|------|
| `core/budget_tracker.py` | **新建** | BudgetTracker 类 (~100 行) |
| `core/budget_parser.py` | **新建** | 预算解析 (~60 行) |
| `agents/codeAgent.py` | **修改** | 集成 BudgetTracker (+30 行) |
| `scripts/chat_test_agent.py` | **修改** | --budget 参数 + /budget 命令 (+25 行) |
| `tests/test_budget_tracker.py` | **新建** | 预算追踪测试 (~120 行) |
| `tests/test_budget_parser.py` | **新建** | 解析测试 (~80 行) |

### 预估

| Phase | 新增行数 | 修改行数 |
|-------|---------|---------|
| BudgetTracker + Parser | ~160 | 0 |
| CodeAgent 集成 | 0 | +30 |
| CLI 集成 | 0 | +25 |
| 测试 | ~200 | 0 |
| **总计** | **~360** | **+55** |

---

## 六、Feature 集成

在架构重构后，预算追踪作为一个 `BudgetFeature` 注册：

```python
class BudgetFeature(AgentFeature):
    name = "budget"
    order = 55

    def init(self, agent):
        agent._budget_tracker = BudgetTracker()

    def runtime_blocks(self, agent, step):
        status = agent._budget_tracker.status_text()
        return [status] if status else []

    def post_tool_use(self, agent, tool_name, tool_input, result):
        # 每次 LLM 调用后扣减（在 _invoke_llm 中处理）
        return []
```
