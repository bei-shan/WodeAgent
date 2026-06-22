# Output Styles 功能设计文档

> 日期: 2026-06-22 | 优先级: P2 | 来源: Kode-Agent 学习计划

---

## 一、功能概述

Output Styles（输出风格）允许用户选择不同的 Agent 交互风格，通过向系统提示注入风格特定的指令来改变 LLM 的输出行为。

三种内置风格：

| 风格 | 效果 | 场景 |
|------|------|------|
| `default` | 保持当前行为，不注入任何内容 | 日常编码 |
| `explanatory` | 完成任务同时解释实现选择 | 学习代码库 |
| `learning` | 留 TODO(human) 让用户动手写代码 | 教学/练习 |

用户也可以创建自定义风格（Markdown 文件）。

---

## 二、Kode-Agent 参考分析

### 2.1 核心机制

```
选择层:  /output-style <name> → .kode/settings.local.json
加载层:  outputStyles.ts → getAvailableOutputStyles() (memoized)
         ├── 内置 (hardcoded): default=null, Explanatory, Learning
         ├── 插件: <plugin>/output-styles/*.md
         ├── 用户: ~/.claude/output-styles/*.md
         └── 项目: .claude/output-styles/*.md (向上遍历)
注入层:  getOutputStyleSystemPromptAdditions()
         → ["\n# Output Style: <name>\n<prompt>\n"]
         → query.ts 追加到 fullSystemPrompt
```

### 2.2 两种内置风格的完整 prompt

**Explanatory:**
```
You are an interactive CLI tool that helps users with software engineering tasks.
In addition to software engineering tasks, you should provide educational insights
about the codebase along the way.

You should be clear and educational, providing helpful explanations while remaining
focused on the task. Balance educational content with task completion.

# Explanatory Style Active

## Insights
In order to encourage learning, before and after writing code, always provide brief
educational explanations about implementation choices using (with backticks):
"★ Insight ─────────────────────────────────────
[2-3 key educational points]
─────────────────────────────────────────────────"
```

**Learning:** Explanatory 的全部 + "Learn by Doing" 交互机制（要求用户写代码，TODO(human) 标记）。

### 2.3 自定义风格格式

```markdown
---
name: my-style
description: 我的自定义风格
keep-coding-instructions: true
---

风格特定的系统提示内容...
```

### 2.4 我们不需要照搬的部分

- **插件系统** (plugin output styles) — MyCodeAgent 无插件体系
- **policySettings** — 企业级功能，暂不需要
- **多级目录发现** (user/project/policy) — v1 只做项目级 `output_styles/`
- **settings.local.json** — 用 env var + session 变量替代

---

## 三、MyCodeAgent 实现设计

### 3.1 架构

```
选择层:
  AGENT_OUTPUT_STYLE=explanatory   (env var, 默认 "default")
  /style explanatory               (slash command, session 级覆盖)
  /style                           (无参数 = 显示当前 + 可用列表)

加载层: core/output_styles.py
  ├── OutputStyleManager
  │   ├── _load_builtin_styles()    → prompts/output_styles/*.md
  │   ├── _load_custom_styles()     → {project_root}/output_styles/*.md
  │   ├── get_style(name)           → OutputStyleDefinition | None
  │   ├── set_current(name)         → session 级覆盖
  │   ├── get_current()             → 返回当前风格名
  │   ├── get_current_prompt()      → 返回风格 prompt (default 返回 "")
  │   └── list_all()                → {name: description}
  └── OutputStyleDefinition (dataclass)
      ├── name: str
      ├── description: str
      ├── prompt: str
      ├── source: "builtin" | "project"
      └── keep_coding_instructions: bool

注入层: L1_system_prompt.py + core/context_engine/context_builder.py
  ├── L1 末尾添加 {output_style} 占位符
  └── ContextBuilder 用风格 prompt 替换占位符 (default → 替换为空)
```

### 3.2 注入方式：{output_style} 占位符替换

**不在 L1 后追加独立 system 消息，而是在 L1 内部替换。** 原因见 3.3。

具体做法：

**L1_system_prompt.py 末尾添加占位符：**
```python
system_prompt = """ ...现有全部内容...
{output_style}
"""
```

**ContextBuilder._load_system_prompt() 中替换：**
```python
prompt = self._load_system_prompt()
# ... tools 替换 ...
if self._output_style_prompt:
    prompt = prompt.replace("{output_style}", self._output_style_prompt)
else:
    prompt = prompt.replace("{output_style}", "")
```

**default 风格的处理：**
- `get_current_prompt()` 对 `default` 返回空字符串 `""`
- `{output_style}` 被替换为空，行为完全不变

### 3.3 为什么用占位符而非独立消息——上下文冲突分析

L1 第 50-52 行有严格的简洁性要求（同一 system 消息内）：
```
IMPORTANT: You MUST answer concisely with fewer than 4 lines...
IMPORTANT: Keep your responses short...
```

而 explanatory 风格要求完全相反的行为：
```
provide educational insights... you may exceed typical length constraints
★ Insight ─────────────────────────────────────
[2-3 key educational points]
```

**如果使用独立 system 消息方案（原设计）：**
- 两条矛盾的硬性指令分别在不同 system 消息中
- 模型可能折中，两边都不做好
- "MUST answer concisely with fewer than 4 lines" 级别的指令不一定能被后一条消息可靠覆盖
- 行为不稳定，不同轮次表现不一致

**使用 {output_style} 占位符方案（新设计）：**
- 风格 prompt 在 L1 的**末尾**注入，同一消息内自然覆盖前面的简洁性要求
- LLM 对同一消息内的指令优先级天然遵循"后面覆盖前面"
- 不产生跨消息的指令冲突
- L1 只需加一行 `{output_style}`，改动最小
- `default` 时替换为空，完全不影响现有行为

### 3.4 风格文件格式

存放在 `prompts/output_styles/` 目录，Markdown + YAML frontmatter：

**explanatory.md:**
```markdown
---
name: explanatory
description: 解释实现选择，适合学习代码库
keep_coding_instructions: true
---

You are an interactive CLI tool that helps users with software engineering tasks.
In addition to software engineering tasks, you should provide educational insights
about the codebase along the way.

You should be clear and educational, providing helpful explanations while remaining
focused on the task. Balance educational content with task completion.

# Explanatory Style Active

## Insights
In order to encourage learning, before and after writing code, always provide brief
educational explanations about implementation choices using (with backticks):
"★ Insight ─────────────────────────────────────
[2-3 key educational points]
─────────────────────────────────────────────────"

These insights should be included in the conversation, not in the codebase.
You should generally focus on interesting insights that are specific to the
codebase or the code you just wrote, rather than general programming concepts.
```

**learning.md:** 与 Kode-Agent 的 Learning 风格内容一致（explanatory 全部 + Learn by Doing + TODO(human)）。

**default.md:** 空 prompt，仅作占位符。

### 3.5 `keep_coding_instructions` 字段

| 值 | 含义 |
|----|------|
| `true` | 保留 L1 中的所有编码规范（Doing tasks, Code style, Following conventions 等） |
| `false` | 风格 prompt 完全替代 L1 的编码规范部分 |

在 v1 中，我们**不动态移除 L1 的部分内容**（改动范围太大）。`keep_coding_instructions` 仅作为元数据记录，供未来版本使用。当前所有内置风格设置为 `true`。

### 3.6 自定义风格

用户可在项目根目录创建 `output_styles/` 目录，放入 `.md` 文件：

```markdown
---
name: concise
description: 比 default 更简洁的输出
keep_coding_instructions: true
---

# Concise Style Active
You MUST answer in 1-2 sentences maximum. No explanations unless asked.
```

加载优先级：项目自定义风格 > 内置风格（同名覆盖）。

### 3.7 文件清单

| 文件 | 操作 | 说明 |
|------|------|------|
| `core/output_styles.py` | **新建** | OutputStyleManager + OutputStyleDefinition |
| `prompts/output_styles/default.md` | **新建** | default 风格（空 prompt） |
| `prompts/output_styles/explanatory.md` | **新建** | 解释风格 |
| `prompts/output_styles/learning.md` | **新建** | 学习风格 |
| `prompts/agents_prompts/L1_system_prompt.py` | **修改** | 末尾添加 `{output_style}` 占位符 |
| `core/context_engine/context_builder.py` | **修改** | `_load_system_prompt()` 替换占位符（+12 行） |
| `agents/codeAgent.py` | **修改** | 初始化 OutputStyleManager，暴露 API |
| `scripts/chat_test_agent.py` | **修改** | `/style` 命令 + StatusLine 集成 |
| `tui/status_line.py` | **修改** | 显示当前风格 |
| `.env.example` | **修改** | 添加 `AGENT_OUTPUT_STYLE` |
| `tests/test_output_styles.py` | **新建** | 17 测试用例

### 3.8 预估行数

| 文件 | 行数 |
|------|------|
| `core/output_styles.py` | ~120 |
| `prompts/output_styles/*.md` (3 files) | ~120 |
| `L1_system_prompt.py` (修改) | +1 |
| `context_builder.py` (修改) | +12 |
| `codeAgent.py` (修改) | +15 |
| `chat_test_agent.py` (修改) | +35 |
| `status_line.py` (修改) | +10 |
| `tests/test_output_styles.py` | ~130 |
| **总计** | **~443** |

---

## 四、API 设计

### 4.1 OutputStyleDefinition

```python
@dataclass
class OutputStyleDefinition:
    name: str                      # "explanatory"
    description: str               # "解释实现选择，适合学习代码库"
    prompt: str                    # 注入到 system prompt 的完整内容
    source: str                    # "builtin" | "project"
    keep_coding_instructions: bool # 是否保留 L1 编码规范（v1 仅记录）
```

### 4.2 OutputStyleManager

```python
class OutputStyleManager:
    def __init__(self, project_root: str, env_style: str | None = None)
    
    # 加载
    def _load_builtin_styles(self) -> dict[str, OutputStyleDefinition]
    def _load_custom_styles(self) -> dict[str, OutputStyleDefinition]
    def _parse_style_file(self, path: Path, source: str) -> OutputStyleDefinition | None
    
    # 查询
    def get_style(self, name: str) -> OutputStyleDefinition | None
    def get_current(self) -> str                           # 当前风格名
    def get_current_prompt(self) -> str                    # 当前风格 prompt (default="")
    def list_all(self) -> dict[str, str]                   # {name: description}
    
    # 设置
    def set_current(self, name: str) -> bool               # 返回是否成功
    def resolve_name(self, name: str) -> str | None        # 大小写不敏感查找
    def reload(self) -> None                               # 重新加载自定义风格
```

### 4.3 CodeAgent 集成

```python
class CodeAgent:
    _output_style_manager: OutputStyleManager  # 新增
    
    @property
    def output_style(self) -> str: ...
    
    def set_output_style(self, name: str) -> bool: ...
    
    def list_output_styles(self) -> dict[str, str]: ...
```

### 4.4 ContextBuilder 集成

```python
class ContextBuilder:
    _output_style_prompt: str = ""  # 新增字段，当前风格的 prompt 文本
    
    def set_output_style_prompt(self, prompt: str) -> None:
        """设置输出风格 prompt，清空 system cache。"""
        self._output_style_prompt = prompt
        self._cached_system_messages = None
    
    # _load_system_prompt() 中:
    # 加载 L1 后，用风格 prompt 替换 {output_style} 占位符
    # default 风格 → 替换为空，行为不变
```

---

## 五、CLI 集成

### 5.1 `/style` 命令

```
/style              → 显示当前风格 + 可用列表
/style explanatory  → 切换到 explanatory 风格
/style learning     → 切换到 learning 风格
/style default      → 恢复默认风格
/style my-custom    → 切换到自定义风格
```

### 5.2 StatusLine 集成

在 status line 中显示当前风格（非 default 时）：

```
[deepseek-v4-pro] [plan] [style:explanatory] [wt:feature-x]
```

### 5.3 环境变量

```bash
# .env
AGENT_OUTPUT_STYLE=explanatory  # 默认 "default"
```

---

## 六、测试计划

### 6.1 单元测试 (`test_output_styles.py`)

| # | 测试 | 说明 |
|---|------|------|
| 1 | `test_default_style_has_empty_prompt` | default 返回空 prompt |
| 2 | `test_explanatory_style_has_prompt` | explanatory 返回非空 prompt |
| 3 | `test_learning_style_has_prompt` | learning 返回非空 prompt |
| 4 | `test_learning_includes_insights` | learning prompt 包含 Insights |
| 5 | `test_learning_includes_learn_by_doing` | learning prompt 包含 Learn by Doing |
| 6 | `test_resolve_name_case_insensitive` | "Explanatory" → "explanatory" |
| 7 | `test_resolve_name_invalid_returns_none` | 无效名称返回 None |
| 8 | `test_set_current_valid` | 设置有效风格成功 |
| 9 | `test_set_current_invalid` | 设置无效风格失败 |
| 10 | `test_list_all_returns_dict` | list_all 返回 dict |
| 11 | `test_custom_style_override_builtin` | 项目自定义覆盖内置 |
| 12 | `test_custom_style_from_md` | 从 Markdown 文件加载自定义风格 |
| 13 | `test_custom_style_missing_frontmatter` | 缺少 frontmatter 使用默认值 |
| 14 | `test_env_var_sets_initial_style` | AGENT_OUTPUT_STYLE 设置初始风格 |
| 15 | `test_context_builder_injects_style_prompt` | ContextBuilder 注入风格 prompt |
| 16 | `test_context_builder_skips_default_style` | default 风格不注入 |
| 17 | `test_reload_discovers_new_styles` | reload 发现新文件 |

### 6.2 集成测试

- `/style` 命令切换成功
- StatusLine 显示当前风格
- CodeAgent 运行后 LLM 响应体现风格差异

---

## 七、实施步骤

```
Step 1: 创建 prompts/output_styles/*.md (3 个风格文件)
Step 2: 创建 core/output_styles.py (OutputStyleManager)
Step 3: 修改 prompts/agents_prompts/L1_system_prompt.py (添加 {output_style} 占位符)
Step 4: 修改 core/context_engine/context_builder.py ({output_style} 替换逻辑)
Step 5: 修改 agents/codeAgent.py (集成 OutputStyleManager)
Step 6: 修改 scripts/chat_test_agent.py (/style 命令)
Step 7: 修改 tui/status_line.py (显示风格)
Step 8: 更新 .env.example
Step 9: 编写测试 test_output_styles.py
Step 10: 运行全量测试验证
```

---

## 八、不做的事情（v1 范围外）

- **插件系统** — MyCodeAgent 无插件体系
- **多级目录发现** (user/policy) — v1 只做项目级 `output_styles/`
- **`keep_coding_instructions` 动态移除 L1** — 仅作元数据记录
- **持久化风格选择** — session 级覆盖，不写文件
- **L1 文件修改** — 不改动现有 L1 提示内容
