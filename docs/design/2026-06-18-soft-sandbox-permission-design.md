# 软沙箱权限系统设计文档

> 状态：已确认
> 日期：2026-06-18
> 目标：将硬沙箱（直接拒绝项目外访问）改为软边界 + 用户确认模式

---

## 零、审阅结论

| 决策点 | 结论 |
|--------|------|
| 权限确认方式 | **直接弹窗**（`input()`），和 Claude Code 一致。不由 LLM 中转 |
| 子代理权限 | **继承主 Agent 的授权缓存**。主 Agent 已授权的路径，子代理自动放行 |
| 永久拒绝路径 | 当前列表合理，**暂不需要用户可配置** |
| 硬沙箱回退 | **保留**：`PERMISSION_SOFT_SANDBOX=false` 恢复当前硬沙箱行为 |

---

## 一、背景与动机

### 1.1 当前行为（硬沙箱）

所有文件工具对项目根目录外的路径**直接拒绝**：

```python
# read_file.py / write_file.py / edit_file.py / bash.py 等
target.relative_to(self._root)  # 抛出 ValueError
# → 返回 ACCESS_DENIED，LLM 被告知"无权访问"
```

**问题**：如果用户想操作项目外的文件（如读取全局配置 `~/.gitconfig`、查看系统日志 `/var/log/`、操作另一个项目），必须重新启动 Agent 并修改 `project_root`。这不符合真实使用场景——用户希望 Agent 像 Claude Code 一样，**默认在工作区内操作，但经用户允许后可以访问任意路径**。

### 1.2 目标行为（软边界 + 用户确认）

```
用户启动 Agent（project_root = ~/projects/myapp/）
  ↓
用户说："帮我把 ~/.gitconfig 里的 user.name 改成 shan"
  ↓
Agent 调用 Read({"path": "~/.gitconfig"})
  ↓
工具检测到 ~/.gitconfig 不在 project_root 内
  ↓
弹出确认：Allow access to ~/.gitconfig? [y/N] 
  ↓ 用户输入 y
  ↓
工具正常执行 → 返回文件内容
  ↓
Agent 调用 Edit 修改 user.name
  ↓
工具再次检测路径 → 本次会话已确认过 → 直接放行（不重复询问）
```

核心原则：
- **默认拒绝，用户授权后放行**
- **同文件只问一次**（会话级缓存）
- **敏感路径永远拒绝**（如系统关键文件、其他用户的 home 目录）
- **子代理继承主 Agent 的授权缓存**（可选，MVP 不实现）

---

## 二、架构设计

### 2.1 整体流程

```
┌─────────────────────────────────────────────────────────────┐
│ 工具执行 run(parameters)                                      │
│   │                                                           │
│   ├─ 1. 路径解析 + 沙箱检查                                    │
│   │     target.relative_to(self._root)                         │
│   │     │                                                     │
│   │     ├─ 成功（在项目内）→ 继续执行                           │
│   │     │                                                     │
│   │     └─ 失败（在项目外）→ 2. 权限检查                        │
│   │                                                           │
│   ├─ 2. 权限检查                                               │
│   │     _permission_gate.check(str(target))                    │
│   │     │                                                     │
│   │     ├─ 已授权（缓存命中）→ 继续执行                         │
│   │     │                                                     │
│   │     ├─ 永久拒绝（敏感路径）→ ACCESS_DENIED                 │
│   │     │                                                     │
│   │     └─ 未授权 → 3. 用户确认                                │
│   │                                                           │
│   └─ 3. 用户确认                                               │
│         _permission_gate.ask(str(target), tool_name, action)   │
│         │                                                     │
│         ├─ 用户同意 → 缓存授权 → 继续执行                       │
│         └─ 用户拒绝 → 缓存拒绝 → ACCESS_DENIED                 │
└─────────────────────────────────────────────────────────────┘
```

### 2.2 新增组件

#### `PermissionGate` 类（`tools/permission_gate.py`）

```python
class PermissionGate:
    """路径访问权限管理器。"""

    def __init__(self, project_root: Path, interactive: bool = True):
        self._project_root = project_root
        self._interactive = interactive
        # 会话级缓存：{resolved_path: "granted" | "denied"}
        self._cache: dict[str, str] = {}
        # 永久拒绝的路径模式
        self._deny_patterns: list[str] = [
            "/etc/shadow", "/etc/sudoers",
            "/System/", "/Windows/System32/",
        ]

    def check(self, resolved_path: str) -> str:
        """
        返回 "granted" | "denied" | "ask"
        """
        ...

    def ask(self, resolved_path: str, tool_name: str, action: str) -> str:
        """
        通过 input() 弹窗询问用户，返回 "granted" | "denied"
        """
        ...
```

#### 权限缓存的生命周期

- **作用域**：当前 Agent 会话（`CodeAgent` 实例生命周期内）
- **存储**：内存 dict，`{解析后的绝对路径: "granted"|"denied"}`
- **跨工具共享**：Read 授权后，Edit 同一文件不再询问
- **不持久化**：会话结束后缓存消失，下次启动重新询问（和 Claude Code 一致）

### 2.3 权限等级

| 等级 | 路径示例 | 行为 |
|------|----------|------|
| ✅ 项目内 | `src/main.py` | 直接放行，不询问 |
| ⚠️ 项目外（可授权） | `~/.gitconfig`, `/tmp/log.txt` | 弹窗询问，用户决定 |
| 🔴 永久拒绝 | `/etc/shadow`, `C:\Windows\System32\*` | 直接拒绝，不询问 |

---

## 三、需要改动的文件

### 3.1 新增文件

| 文件 | 行数 | 说明 |
|------|------|------|
| `tools/permission_gate.py` | ~80 | `PermissionGate` 类：检查、询问、缓存 |

### 3.2 修改文件

| 文件 | 改动 | 说明 |
|------|------|------|
| `tools/base.py` | +3 行 | `Tool.__init__` 增加 `permission_gate` 可选参数 |
| `tools/builtin/read_file.py` | ~5 行 | `relative_to` 失败后走 `PermissionGate.check/ask` |
| `tools/builtin/write_file.py` | ~5 行 | 同上 |
| `tools/builtin/edit_file.py` | ~5 行 | 同上 |
| `tools/builtin/edit_file_multi.py` | ~5 行 | 同上 |
| `tools/builtin/bash.py` | ~5 行 | cd 路径检查改软边界 |
| `tools/builtin/search_code.py` | ~5 行 | Grep 的路径检查 |
| `tools/builtin/search_files_by_name.py` | ~5 行 | Glob 的路径检查 |
| `tools/builtin/list_files.py` | ~5 行 | LS 的路径检查 |
| `agents/codeAgent.py` | ~10 行 | 创建 `PermissionGate` 实例，注入到 `ToolRegistry` 的工具中 |

**总计：1 个新文件 + 9 个文件改动，约 50 行核心代码。**

### 3.3 不改的文件

| 文件 | 原因 |
|------|------|
| `tools/builtin/todo_write.py` | 只写 `memory/todos/`，永远在项目内 |
| `tools/builtin/skill.py` | 只读 `skills/` 目录，永远在项目内 |
| `tools/builtin/ask_user.py` | 本身是权限机制的一部分，不需要权限检查 |
| `tools/builtin/task.py` | 子代理通过 `TurnExecutor` 执行，`TurnExecutor` 使用独立的 `ToolRegistry`——见 4.2 |
| `tools/mcp/adapter.py` | MCP 工具操作的是远程资源，没有本地路径概念 |
| `tools/registry.py` | 只负责执行调度，路径权限由各工具自行处理 |

---

## 四、关键设计决策

### 4.1 为什么不在 ToolRegistry 统一拦截

**方案 A（Registry 拦截）**：

```python
# registry.py execute_tool() 中统一检查
if name in {"Read", "Write", "Edit", ...}:
    path = parameters.get("path")
    if not is_in_project(path):
        permission_gate.check(path)
```

**方案 B（工具各自检查）**：

```python
# 每个工具的 run() 中各自检查
try:
    target.relative_to(self._root)
except ValueError:
    result = self._permission_gate.check_and_ask(...)
```

**选择方案 B**。理由：
- 每个工具的路径解析逻辑不同（Read 用 `path`，Bash 用 `directory` + `cd` 检测，Grep 用 `include`）
- Registry 不知道每个工具的路径参数叫什么、怎么解析
- 方案 B 改动集中在已有的 `relative_to` 异常处理处，改动最小

### 4.2 子代理（TurnExecutor / Task）的权限处理

子代理**继承主 Agent 的 `PermissionGate` 实例**（共享同一个缓存）。

- 主 Agent 已授权的路径 → 子代理自动放行，不重复询问
- 子代理访问**新路径**时：
  - **主 Agent 已运行过 `ask()`**：缓存命中 → 直接放行
  - **主 Agent 未运行过 `ask()`**：子代理设置 `interactive=False` → 返回 `ACCESS_DENIED`，由主 Agent 处理

实现方式：
- `TaskTool._create_filtered_registry()` 创建子代理的 `ToolRegistry` 时，注册的工具**使用相同的 `PermissionGate` 实例**
- `PermissionGate` 的 `_cache` 是内存 dict，主 Agent 和子代理共享引用
- 子代理的 `PermissionGate.ask()` 被跳过（`interactive=False`）——弹窗只在主 Agent 的 UI 线程发生

### 4.3 敏感路径的永久拒绝

配置一个硬编码的拒绝列表：

```python
_ALWAYS_DENY_PATTERNS = [
    # Unix
    "/etc/shadow", "/etc/sudoers", "/etc/ssh/",
    "/root/", "/proc/", "/sys/",
    # macOS
    "/System/", "/Library/Keychains/",
    # Windows
    "C:\\Windows\\System32\\", "C:\\Windows\\System\\",
    # 通用
    ".ssh/id_rsa", ".aws/credentials",
]
```

这些路径即使 `PermissionGate.ask()` 也不会被调用——`check()` 直接返回 `"denied"`。用户可以通过修改 `_ALWAYS_DENY_PATTERNS` 或环境变量 `PERMISSION_ALWAYS_DENY` 自定义。

### 4.4 权限提示的 UI 设计

和 Claude Code 一致，在工具执行层直接弹窗，LLM 无感知：

```
═══════════════════════════════════════════════════════
  🔒 权限请求
  Read 工具尝试访问项目外的文件:
  C:\Users\shan\.gitconfig
  操作: 读取
  允许访问? [y/N/a] (y=本次, a=本次会话全部允许)
═══════════════════════════════════════════════════════
```

**为什么是直接弹窗而不是 LLM 中转**：

Claude Code 的做法是工具执行层拦截——LLM 不知道弹窗发生了，只看到工具返回了结果。如果让 LLM 通过 `AskUserTool` 中转：
- LLM 可能"说服"用户授权不该授权的路径（社会工程风险）
- 增加一轮 LLM 调用的延迟和 token 消耗
- 权限是基础设施层决策，LLM 不应参与

**交互模式**：
- 交互模式：`input()` 等待用户输入（y/n/a）
- 非交互模式（`AGENT_INTERACTIVE=false`）：直接返回 `ACCESS_DENIED`，附带提示"需要交互模式才能授权"
- 子代理：`PermissionGate` 设为 `interactive=False`，不弹窗，新路径直接拒绝

### 4.5 环境变量控制

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `PERMISSION_SOFT_SANDBOX` | `true` | `true`=软边界+确认, `false`=硬沙箱（当前行为） |
| `PERMISSION_CACHE_SIZE` | `500` | 会话级缓存最大条目数 |
| `PERMISSION_ALWAYS_DENY` | — | 追加的永久拒绝路径（逗号分隔） |

---

## 五、实现步骤

### Phase 1：核心组件

1. 创建 `tools/permission_gate.py`
2. 在 `Tool.__init__` 中添加 `permission_gate` 可选参数（`base.py`）
3. 在 `CodeAgent.__init__` 中创建 `PermissionGate`，注入到 `ToolRegistry` 中的工具

### Phase 2：工具适配

4. 修改 `read_file.py`：`relative_to` 失败 → `PermissionGate.check/ask`
5. 修改 `write_file.py`：同上
6. 修改 `edit_file.py`：同上
7. 修改 `edit_file_multi.py`：同上
8. 修改 `bash.py`：cd 路径检查改软边界
9. 修改 `search_code.py`（Grep）、`search_files_by_name.py`（Glob）、`list_files.py`（LS）

### Phase 3：测试

10. 测试 `PermissionGate` 基本逻辑（检查、询问、缓存）
11. 测试永久拒绝路径
12. 测试非交互模式（返回 `ACCESS_DENIED`）
13. 测试跨工具缓存（Read 授权后 Edit 不再询问）
14. 回归测试所有文件工具

---

## 六、风险与边界情况

| 风险 | 缓解措施 |
|------|----------|
| 符号链接绕过（`~/link → /etc/shadow`） | `Path.resolve()` 已解析符号链接，再检查 `_ALWAYS_DENY_PATTERNS` |
| TOCTOU（路径检查后文件被替换） | 和现有乐观锁机制一致——双重检查 |
| 非交互模式无法确认 | 返回 `ACCESS_DENIED` + 明确提示，LLM 可以告知用户"需要切换为交互模式" |
| 缓存膨胀 | `PERMISSION_CACHE_SIZE` 限制，超过后淘汰最旧的条目 |
| 用户误操作授权敏感路径 | `_ALWAYS_DENY_PATTERNS` 硬拒绝作为最后防线 |

---

## 七、与 Claude Code 的对比

| 特性 | Claude Code | 本设计 |
|------|-------------|--------|
| 默认边界 | workspace 目录 | project_root 目录 |
| 越界行为 | 弹窗确认（GUI/终端） | 终端 `input()` 确认 |
| 缓存范围 | 会话级 | 会话级 |
| 敏感路径 | 硬拒绝（不弹窗） | 硬拒绝（`_ALWAYS_DENY_PATTERNS`） |
| 子代理权限 | 继承主会话 | MVP 不继承（`interactive=False`） |
| 持久化 | 不持久化 | 不持久化 |
| 环境变量控制 | `--dangerously-skip-permissions` | `PERMISSION_SOFT_SANDBOX=false` |

---

## 八、审阅要点

请审阅以下关键决策：

1. **权限确认方式**：用 `input()` 直接弹窗 vs 通过 `AskUserTool` 让 LLM 中转？
   - 本设计选 `input()`——权限是基础设施层决策，不应由 LLM 做

2. **子代理权限**：MVP 选 `interactive=False`（子代理不弹窗），后续是否支持继承？

3. **永久拒绝列表**：当前的 `_ALWAYS_DENY_PATTERNS` 是否合理？是否需要让用户可配置？

4. **环境变量开关**：是否需要 `PERMISSION_SOFT_SANDBOX=false` 来保留当前硬沙箱行为？
