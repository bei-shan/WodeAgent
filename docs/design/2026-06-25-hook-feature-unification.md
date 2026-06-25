# Hook + Feature 拦截统一设计

> 日期: 2026-06-25 | 审计编号: P1 #11 | 难度: 大

---

## 一、现状诊断

### 当前 `_execute_tool` 有 3 层互不统属的拦截

```python
def _execute_tool(self, tool_name, tool_input):
    # 第 1 层：Delegate mode — 硬编码在 CodeAgent
    if not self._is_tool_allowed_in_delegate_mode(tool_name):
        return error_response

    # 第 2 层：Feature pre_tool_use 循环 — 11 个 Feature 逐个问
    for feat in self._features:
        pre_result = feat.pre_tool_use(self, tool_name, normalized_input)
        # 实际只有 HookFeature 会返回非 None

    # 第 3 层：Plan mode 工具过滤 — 在 _get_openai_tools_for_current_mode 中
    # 不是拦截，是预先从 tools schema 里移除
```

**问题**：
1. Delegate mode 的拦截逻辑硬编码在 `_execute_tool` 中，Feature 协议空转
2. 11 个 Feature 遍历下来实际只有 HookFeature 会拦截，浪费
3. 三层拦截各自为政，没有统一的优先级和短路语义

### 目标架构

```
_execute_tool:
  1. Feature pre_tool_use 统一入口（DelegateModeFeature + HookFeature 都在此）
  2. 执行工具
  3. Feature post_tool_use 统一入口
```

---

## 二、设计

### Step 1: DelegateModeFeature 实现 pre_tool_use

```python
# core/features/delegate.py

class DelegateModeFeature(AgentFeature):
    def pre_tool_use(self, agent, tool_name, tool_input):
        if not agent.delegate_mode:
            return None  # 放行
        if tool_name in agent.DELEGATION_ALLOWED_TOOLS:
            return None  # 在白名单中，放行
        return {
            "blocked": True,
            "reason": f"Tool '{tool_name}' is not allowed in delegate mode.",
        }
```

### Step 2: CodeAgent._execute_tool 移除硬编码拦截

```python
# 删除:
if not self._is_tool_allowed_in_delegate_mode(tool_name):
    return error_response
```

### Step 3: Feature 拦截结果优先级统一

```python
# Feature 返回语义明确化:
# None          → 放行
# {"blocked": True, "reason": "..."}    → 阻止
# {"blocked": False, "updated_input": {...}, "system_messages": [...]} → 修改通过
```

### Step 4: `_is_tool_allowed_in_delegate_mode` 保留为兼容方法（deprecated 标记）

---

## 三、文件清单

| 文件 | 操作 | 行数 |
|------|------|------|
| `core/features/delegate.py` | 修改：添加 pre_tool_use | +12 |
| `agents/codeAgent.py` | 修改：删除硬编码拦截，简化 _execute_tool | -18 |
| `tests/test_plan_mode_background.py` | 确认无需修改（plan mode 通过 schema 过滤） | — |

## 四、风险评估

- **低风险**：Delegate mode 行为不变，只是换了实现位置
- 测试覆盖：`test_plan_mode_background.py` 已覆盖 plan mode 过滤
- delegate mode 测试需要验证 Feature 拦截是否生效
