# 第 2 批修复设计方案

> 日期: 2026-06-26 | 审计编号: P0 #5, P0 #6, P1 #9, P1 #13

---

## 一、P0 #6 — Bash 长输出截断

### 现状

`bash.py:288` — `"truncated": False,  # MVP 阶段不截断`

所有成功的 Bash 输出都原样发送，不管多大。`text` 字段人工截到 2000 字符但 `data.stdout` 仍然是完整的。ObservationTruncator 会二次截断，但截断时机在 history 写入时，LLM 看到的已经是截断后的。问题是 data 里的完整 stdout 浪费了 tool-output 落盘空间且 text 字段做了硬编码截断。

### 修复方案

**不引入新的截断层**，而是利用已有的 ObservationTruncator。当前 Bash 工具已经通过 `append_tool()` → `truncate_observation()` 获得了截断保护。只需要：

1. 设置 `data.truncated = True` 当输出超过阈值（和 ObservationTruncator 的 2000 行/50KB 一致）
2. `text` 中提示完整输出路径（由 ObservationTruncator 提供）
3. 去除 text 字段中的硬编码 `stdout[:2000]`/`stderr[:1000]` 截断，由 ObservationTruncator 统一处理

**实际改动**（~10 行）：
```python
# bash.py
data: Dict[str, Any] = {
    ...
    "truncated": False,  # 由 ObservationTruncator 统一处理
    ...
}

# text 字段中不截断 stdout/stderr，完整输出给 ObservationTruncator
if stdout:
    text_lines.append(f"\n--- STDOUT ({len(stdout.encode('utf-8'))} bytes) ---")
    text_lines.append(stdout)  # 不再加 [:2000]
```

**风险评估**：零风险。ObservationTruncator 已经在 `append_tool()` 中拦截，Bash 工具无需关心截断逻辑。

---

## 二、P0 #5 — Bash 软沙箱加固

### 现状

Bash 工具的安全检查在 `_check_command_safety()` 中。当前拦截了：
- 命令替换（`$()` / backticks）
- 输出重定向到绝对路径
- 交互式命令（vim/nano/top 等）
- 破坏性命令（mkfs/dd 等）
- 权限提升（sudo/su）
- rm -rf /
- 远程脚本执行（curl|bash）
- 网络工具（curl/wget，可配置允许）
- 读/搜/列类命令（ls/cat/grep/find 等，引导用工具）

**缺失**：`python -c "open('path', 'w').write('evil')"` 可以绕过所有规则写文件。

### 修复方案

**不拦截 python 本身**（破坏合理的 python 脚本执行）。而是在软沙箱层（PermissionGate）增加防御深度：

1. **Bash 工具内**：新增一个可配置的**写操作拦截列表**，检查命令中是否包含文件写入操作：
   ```
   python -c "open(...)"     → ⚠️ 检测到 open() 调用，警告用户确认
   python -c "...write(...)"→ ⚠️ 同上
   echo "..." > file         → ⚠️ 输出重定向到相对路径也需要警告
   ```

2. **方案**：在 `_check_command_safety()` 中新增一个**中等风险检查**。不是硬阻止，而是要求 `PERMISSION_SOFT_SANDBOX=true` 下弹出确认。这样：
   - 合理的 `python -c "print('hello')"` → 放行
   - 可疑的 `python -c "open('/etc/hosts','w')"` → 用户确认
   - 明显的 `rm -rf /` → 硬阻止

### 具体规则（3 条新增）

```python
# 1. python -c 中检测文件写入
PYTHON_WRITE_PATTERNS = [
    r'\bopen\s*\(.*[\'"]w[\'"]',   # open(..., 'w')
    r'\bopen\s*\(.*[\'"]a[\'"]',   # open(..., 'a')  
    r'\.write\s*\(',                # .write()
    r'\.writelines\s*\(',           # .writelines()
    r'pathlib.*write_text',         # Path.write_text()
    r'shutil\.(copy|move)',         # shutil file ops
]

# 2. 输出重定向到相对路径（项目内），配合 PERMISSION_SOFT_SANDBOX 确认
#    > file, >> file, 2> file, &> file, 1> file
# 3. tee 命令到文件
```

**风险评估**：低。只拦截 python/sh 写文件操作，不影响 `python -c "import os; print(os.getcwd())"` 这类只读操作。误报率预计低于 5%。

---

## 三、P1 #13 — 删除 `_is_minimax_backend` 硬编码域名

### 现状

```python
# core/llm.py:321
def _is_minimax_backend(self) -> bool:
    base = (self.base_url or "").lower()
    return "minimaxi.com" in base or "minimax.io" in base
```

这家公司的域名被硬编码在开源项目里。后续它们改域名、或者其他厂商也需要类似 patch 时要再改代码。

### 修复方案

**改为 Provider Profile 的 `quirks` 属性**：

```python
# core/llm.py — 删除 _is_minimax_backend

# 改为检查 provider 的 quirks 配置
QUIRKS_MAP = {
    "minimax": {"force_n_1": True, "no_tool_choice_auto": True},
}

def _apply_provider_compat(self, request_kwargs):
    quirks = QUIRKS_MAP.get((self.provider or "").lower().strip(), {})
    if quirks.get("force_n_1"):
        request_kwargs["n"] = 1
    if quirks.get("no_tool_choice_auto"):
        request_kwargs.pop("tool_choice", None)
    return request_kwargs
```

新增 provider 厂商 quirks 时，只需在 `QUIRKS_MAP` 里加一行，不需要改函数逻辑。

**风险评估**：中等。需确认 `self.provider` 在 Minimax 使用时被正确设置。向后兼容：`_apply_provider_compat` 签名不变，行为一致。

---

## 四、P1 #9 — 删除双份工具定义

### 现状

```python
# codeAgent.py:52 — 硬编码第一份
DELEGATION_ALLOWED_TOOLS = {"TeamCreate", "SendMessage", ...}

# codeAgent.py:70 — 硬编码第二份
PLAN_MODE_TOOLS = {"Read", "Grep", "Glob", "LS", ...}

# core/features/delegate.py:19 — Feature 里也 set 一份
agent.DELEGATION_ALLOWED_TOOLS = ...

# core/features/plan_mode.py:23 — Feature 里也 set 一份
agent.PLAN_MODE_TOOLS = ...
```

四份定义，两份相同逻辑。Feature 的定义是运行时注入的，但 CodeAgent 的类属性是静态的。如果 Feature 未初始化，CodeAgent 的类属性作为 fallback。

### 修复方案

**让 Feature 成为唯一权威来源**，删除 CodeAgent 类属性中的硬编码集合：

```python
# codeAgent.py:52 → 删除 DELEGATION_ALLOWED_TOOLS 类属性
# codeAgent.py:70 → PLAN_MODE_TOOLS 改为空集合 {}，由 PlanModeFeature.init() 填充

# 工具过滤逻辑增加 fallback：
def _is_tool_allowed_in_delegate_mode(self, name: str) -> bool:
    allowed = getattr(self, "DELEGATION_ALLOWED_TOOLS", set())
    return name in allowed

def _is_tool_allowed_in_plan_mode(self, name: str) -> bool:
    allowed = getattr(self, "PLAN_MODE_TOOLS", set())
    return name in allowed
```

Feature.init() 中保留赋值：
```python
# plan_mode.py
agent.PLAN_MODE_TOOLS = self.PLAN_MODE_TOOLS  # 不改

# delegate.py
agent.DELEGATION_ALLOWED_TOOLS = self.ALLOWED_TOOLS  # 不改
```

**风险评估**：低。Feature 的 init 在工具注册之前执行，`PLAN_MODE_TOOLS` 在首次 ReAct 循环前一定已设置。CodeAgent 类属性中保留空集合作为安全 fallback。

---

## 五、文件清单

| 文件 | 操作 | 说明 |
|------|------|------|
| `tools/builtin/bash.py` | 修改 | 移除硬编码 text 截断，新增 python 写文件检测 (~20 行) |
| `core/llm.py` | 修改 | 删除 `_is_minimax_backend`，改为 QUIRKS_MAP (~15 行) |
| `agents/codeAgent.py` | 修改 | 删除 DELEGATION_ALLOWED_TOOLS / PLAN_MODE_TOOLS 硬编码 (~5 行) |
| `tests/test_bash_tool.py` | 修改 | 新增截断 + 安全检测测试 (~20 行) |

## 六、预估

| 改动 | 行数 |
|------|------|
| Bash 截断 | +5 |
| Bash 安全 | +20 |
| minimax 重构 | -10 +15 |
| 双份工具定义 | -20 |
| 测试 | +20 |
| **总计** | **~30 新增，~30 删除** |
