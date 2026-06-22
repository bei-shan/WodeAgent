# CodeAgent 架构优化设计文档

> 日期: 2026-06-22 | 优先级: P0-P1 | 范围: 初始化瘦身 + 配置统一 + 运行时重构

---

## 一、问题诊断

### 1.1 CodeAgent.__init__ 膨胀

当前 `__init__` 约 150 行，每加一个功能就往里塞 10-15 行。初始化顺序隐含依赖：

```
self.project_root (必须最先)
  → self.tool_registry (注册工具)
    → self.team_manager (依赖 tool_registry)
    → self._permission_gate (依赖 project_root)
    → self.context_builder (依赖 tool_registry + mcp/skills prompt)
      → self._output_style_manager (依赖 context_builder.set_output_style_prompt)
      → self._hook_manager (依赖 project_root)
      → self._session_manager (依赖 project_root)
```

**问题：** 没有显式依赖声明，靠代码顺序保证。新功能容易放错位置。

### 1.2 配置散落

`os.getenv()` 直接调用散落在 **15 个文件、55 处**，Config 类只覆盖了部分：

| 配置来源 | 数量 | 示例 |
|----------|------|------|
| `Config.from_env()` | 17 项 | context_window, compression_threshold, ... |
| 直接 `os.getenv()` | ~38 处 | AGENT_OUTPUT_STYLE, VCR_ENABLED, WORKTREE_STORE_DIR, ... |

**问题：** 新增配置没有统一入口，`.env.example` 靠手动同步，容易遗漏。

### 1.3 Runtime Blocks 堆积

`_react_loop` 中有 7 个顺序 if 分支往 `runtime_blocks` 追加，每个功能的注入逻辑直接写在循环体里：

```python
if self.enable_agent_teams and ...:    # AgentTeams
    runtime_blocks.extend(...)
if bg_summary:                          # Background Task
    runtime_blocks.append(...)
if self._plan_text:                     # Plan Mode
    runtime_blocks.append(...)
if self._hook_system_messages:          # Hook System
    runtime_blocks.extend(...)
if step == 1 and self._hook_session_context:  # Hook SessionStart
    runtime_blocks.extend(...)
```

**问题：** 新功能的上下文注入需要修改 `_react_loop`，不是开闭原则。

### 1.4 工具执行拦截堆积

`_execute_tool` 中也有类似问题：

```python
if not self._is_tool_allowed_in_delegate_mode(...):  # Delegate mode
    return error
if self._hook_manager.has_any_hooks:                 # PreToolUse hook
    ...
res = self.tool_registry.execute_tool(...)
if self._hook_manager.has_any_hooks:                 # PostToolUse hook
    ...
```

---

## 二、目标架构

```
CodeAgent.__init__ (30 行)
  ├── _init_core()        # project_root, llm, tool_registry, logger
  ├── _init_context()     # HistoryManager, ContextBuilder, SummaryCompressor
  ├── _init_features()    # 遍历 FEATURES 列表，每个 Feature 自注册
  └── _post_init()        # 注册后钩子

CodeAgent._react_loop (干净)
  ├── _collect_runtime_blocks(step)   # 各 Feature 贡献上下文
  ├── _invoke_llm_with_interception() # VCR 拦截在此
  └── _execute_tool_with_interception() # Hook 拦截在此

CodeAgent._execute_tool (干净)
  ├── _check_delegate_mode()
  ├── _run_pre_tool_hooks()
  ├── tool_registry.execute_tool()
  └── _run_post_tool_hooks()

Feature 协议:
  class AgentFeature(ABC):
      name: str
      order: int = 50          # 初始化顺序
      def init(self, agent)    # 设置 self._xxx 属性
      def post_init(self, agent)  # 注册后钩子
      def runtime_blocks(self, agent, step) -> list[str]  # 上下文注入
      def cleanup(self, agent)  # 清理
```

---

## 三、详细设计

### 3.1 AgentFeature 协议

```python
# core/features/base.py (新建)
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from agents.codeAgent import CodeAgent


class AgentFeature(ABC):
    """可插拔的 Agent 功能模块。

    每个 Feature 代表一个可独立启用的功能，通过统一的协议与 CodeAgent 交互。
    所有交互都通过 agent 参数进行，Feature 不持有对 CodeAgent 的强引用。
    """

    name: str = "base"
    order: int = 50  # 初始化顺序，越小越先

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    def init(self, agent: "CodeAgent") -> None:
        """初始化：在 agent 上设置属性和状态。

        在 core 初始化完成后、post_init 之前调用。
        默认空实现 — 不需要初始化的 Feature 不覆写。
        """

    def post_init(self, agent: "CodeAgent") -> None:
        """注册后钩子：在 context_builder 等 core 组件就绪后调用。

        用于需要访问 context_builder 的初始化（如 OutputStyle）。
        默认空实现。
        """

    def cleanup(self, agent: "CodeAgent") -> None:
        """清理：在 agent.close() 时调用。

        默认空实现。
        """

    # ------------------------------------------------------------------
    # 运行时
    # ------------------------------------------------------------------

    def runtime_blocks(self, agent: "CodeAgent", step: int) -> list[str]:
        """返回需要注入到当前步骤 system prompt 的文本块。

        每次 ReAct 步骤都会调用。返回空列表表示无注入。
        """
        return []

    def pre_tool_use(self, agent: "CodeAgent", tool_name: str, tool_input: dict) -> dict | None:
        """工具调用前的拦截。返回 None 表示放行，返回 dict 表示阻止。

        返回格式: {"blocked": True, "reason": "...", "updated_input": {...}}
        """
        return None

    def post_tool_use(self, agent: "CodeAgent", tool_name: str, tool_input: dict, result: str) -> list[str]:
        """工具调用后的拦截。返回要注入的 system_messages 列表。"""
        return []

    def llm_intercept(self, agent: "CodeAgent", messages: list, tools: list, tool_choice: str, fallback) -> Any:
        """LLM 调用拦截。返回 fallback() 的结果或缓存结果。"""
        return fallback()
```

### 3.2 现有功能迁移为 Feature

每个现有功能变成一个 Feature 类：

| Feature | order | 迁移来源 |
|---------|-------|---------|
| `AgentTeamsFeature` | 30 | `__init__` L132-155 + runtime_blocks |
| `WorktreeFeature` | 20 | `__init__` L157-165 |
| `PlanModeFeature` | 60 | `__init__` L167-169 + runtime_blocks + 工具过滤 |
| `BackgroundTaskFeature` | 70 | `__init__` L171-173 + runtime_blocks |
| `OutputStyleFeature` | 80 | `__init__` L214-220 + post_init |
| `VCRFeature` | 90 | `__init__` L222-223 + llm_intercept |
| `HookFeature` | 85 | `__init__` L225-234 + runtime_blocks + pre/post_tool_use |
| `SessionFeature` | 100 | `__init__` L236-239 + auto-save |
| `DelegateModeFeature` | 40 | pre_tool_use + 工具过滤 |

### 3.3 CodeAgent 新 __init__

```python
# agents/codeAgent.py
FEATURES: list[type[AgentFeature]] = [
    WorktreeFeature,
    AgentTeamsFeature,
    DelegateModeFeature,
    PlanModeFeature,
    BackgroundTaskFeature,
    OutputStyleFeature,
    HookFeature,
    VCRFeature,
    SessionFeature,
]

class CodeAgent(Agent):
    def __init__(self, name, llm, tool_registry, project_root, ...):
        super().__init__(name, llm, ...)
        # === Core ===
        self._init_core(project_root, tool_registry, config)
        self._init_context(llm, config)

        # === Features ===
        self._features: list[AgentFeature] = []
        for feat_cls in sorted(FEATURES, key=lambda f: f.order):
            feat = feat_cls()
            feat.init(self)
            self._features.append(feat)

        for feat in self._features:
            feat.post_init(self)

        # === Trace ===
        self.trace_logger = create_trace_logger()
        # ...
```

### 3.4 ReAct 循环瘦身

```python
def _react_loop(self, pending_input, show_raw, trace_logger):
    for step in range(1, self.max_steps + 1):
        # 聚合所有 Feature 的 runtime blocks
        runtime_blocks = []
        for feat in self._features:
            runtime_blocks.extend(feat.runtime_blocks(self, step))

        if runtime_blocks:
            self.context_builder.set_runtime_system_blocks(runtime_blocks)

        # LLM 调用（经过所有 Feature 的 llm_intercept 链）
        result = self._invoke_llm_with_interception(messages, ...)

        # 工具执行（经过 Feature 的 pre/post_tool_use）
        if tool_calls:
            self._execute_step_tools(tool_calls, ...)
```

```python
def _invoke_llm_with_interception(self, messages, tools_schema, tool_choice, ...):
    """LLM 调用链：遍历 Feature 的 llm_intercept，最后 fallback 到真实 API。"""
    fallback = lambda: self.llm.invoke_raw(messages, tools=tools_schema, tool_choice=tool_choice)
    for feat in reversed(self._features):  # 后注册的先包装（洋葱模型）
        prev = fallback
        fallback = lambda f=feat, p=prev: f.llm_intercept(self, messages, tools_schema, tool_choice, p)
    return fallback()
```

```python
def _execute_tool(self, tool_name, tool_input):
    # Pre-tool interception
    for feat in self._features:
        result = feat.pre_tool_use(self, tool_name, tool_input)
        if result and result.get("blocked"):
            return error_json(result["reason"])
        if result and result.get("updated_input"):
            tool_input = {**tool_input, **result["updated_input"]}

    res = self.tool_registry.execute_tool(tool_name, tool_input)

    # Post-tool interception
    for feat in self._features:
        msgs = feat.post_tool_use(self, tool_name, tool_input, str(res))
        self._hook_system_messages.extend(msgs)

    return str(res)
```

### 3.5 统一配置管理

```python
# core/config.py
class Config(BaseModel):
    # === LLM ===
    llm_provider: str = "openai"
    llm_model_id: str = "gpt-3.5-turbo"
    llm_api_key: str = ""
    llm_base_url: str = ""
    llm_timeout: int = 120
    temperature: float = 0.7
    max_tokens: int | None = None

    # === System ===
    debug: bool = False
    log_level: str = "INFO"
    show_react_steps: bool = True
    show_progress: bool = True

    # === Context ===
    context_window: int = 128000
    compression_threshold: float = 0.8
    min_retain_rounds: int = 10
    summary_timeout: int = 120

    # === AgentTeams ===
    enable_agent_teams: bool = False
    agent_teams_store_dir: str = ".teams"
    agent_tasks_store_dir: str = ".tasks"
    teammate_mode: str = "auto"
    delegate_mode: bool = False
    team_max_inbox_size: int = 10000
    team_max_work_items: int = 5000
    team_llm_max_concurrency: int = 4

    # === Subagent ===
    subagent_max_steps: int = 50
    light_llm_model_id: str = ""
    light_llm_api_key: str = ""
    light_llm_base_url: str = ""

    # === Permission ===
    permission_soft_sandbox: bool = True
    agent_interactive: bool = True
    permission_cache_size: int = 500

    # === MCP ===
    mcp_connect_mode: str = "manual"
    mcp_connect_timeout: int = 30
    mcp_call_timeout: int = 30

    # === Worktree ===
    worktree_store_dir: str = ".worktrees"
    worktree_base_ref: str = "fresh"

    # === Output Style ===
    output_style: str = "default"

    # === VCR ===
    vcr_enabled: bool = False
    vcr_record_mode: str = "new_episodes"
    vcr_fixture_dir: str = "tests/fixtures/vcr"

    # === Trace ===
    trace_enabled: bool = True
    trace_dir: str = "memory/traces"
    trace_sanitize: bool = True

    # === Tool Output ===
    tool_output_max_lines: int = 2000
    tool_output_max_bytes: int = 51200
    tool_output_truncate_direction: str = "head"
    tool_output_dir: str = "tool-output"
    tool_output_retention_days: int = 7

    # === Skills ===
    skills_refresh_on_call: bool = True
    skills_prompt_char_budget: int = 12000

    # === Circuit Breaker ===
    circuit_failure_threshold: int = 3
    circuit_recovery_timeout: int = 300

    @classmethod
    def from_env(cls) -> "Config":
        """从环境变量加载所有配置。每个字段有明确的 env var 映射。"""
        return cls(
            # LLM
            llm_provider=_env_str("LLM_PROVIDER", "openai"),
            llm_model_id=_env_str("LLM_MODEL_ID", "gpt-3.5-turbo"),
            llm_api_key=_env_str("LLM_API_KEY", ""),
            llm_base_url=_env_str("LLM_BASE_URL", ""),
            llm_timeout=_env_int("LLM_TIMEOUT", 120),
            temperature=_env_float("TEMPERATURE", 0.7),
            max_tokens=_env_int_optional("MAX_TOKENS"),
            # System
            debug=_env_bool("DEBUG", False),
            log_level=_env_str("LOG_LEVEL", "INFO"),
            show_react_steps=_env_bool("SHOW_REACT_STEPS", True),
            show_progress=_env_bool("SHOW_PROGRESS", True),
            # Context
            context_window=_env_int("CONTEXT_WINDOW", 128000),
            compression_threshold=_env_float("COMPRESSION_THRESHOLD", 0.8),
            min_retain_rounds=_env_int("MIN_RETAIN_ROUNDS", 10),
            summary_timeout=_env_int("SUMMARY_TIMEOUT", 120),
            # AgentTeams
            enable_agent_teams=_env_bool("ENABLE_AGENT_TEAMS", False),
            agent_teams_store_dir=_env_str("AGENT_TEAMS_STORE_DIR", ".teams"),
            agent_tasks_store_dir=_env_str("AGENT_TASKS_STORE_DIR", ".tasks"),
            teammate_mode=_env_str("TEAMMATE_MODE", "auto"),
            delegate_mode=_env_bool("TEAM_DELEGATE_MODE", False),
            team_max_inbox_size=_env_int("TEAM_MAX_INBOX_SIZE", 10000),
            team_max_work_items=_env_int("TEAM_MAX_WORK_ITEMS", 5000),
            team_llm_max_concurrency=_env_int("TEAM_LLM_MAX_CONCURRENCY", 4),
            # Subagent
            subagent_max_steps=_env_int("SUBAGENT_MAX_STEPS", 50),
            light_llm_model_id=_env_str("LIGHT_LLM_MODEL_ID", ""),
            light_llm_api_key=_env_str("LIGHT_LLM_API_KEY", ""),
            light_llm_base_url=_env_str("LIGHT_LLM_BASE_URL", ""),
            # Permission
            permission_soft_sandbox=_env_bool("PERMISSION_SOFT_SANDBOX", True),
            agent_interactive=_env_bool("AGENT_INTERACTIVE", True),
            permission_cache_size=_env_int("PERMISSION_CACHE_SIZE", 500),
            # MCP
            mcp_connect_mode=_env_str("MCP_CONNECT_MODE", "manual"),
            mcp_connect_timeout=_env_int("MCP_CONNECT_TIMEOUT", 30),
            mcp_call_timeout=_env_int("MCP_CALL_TIMEOUT", 30),
            # Worktree
            worktree_store_dir=_env_str("WORKTREE_STORE_DIR", ".worktrees"),
            worktree_base_ref=_env_str("WORKTREE_BASE_REF", "fresh"),
            # Output Style
            output_style=_env_str("AGENT_OUTPUT_STYLE", "default"),
            # VCR
            vcr_enabled=_env_bool("VCR_ENABLED", False),
            vcr_record_mode=_env_str("VCR_RECORD_MODE", "new_episodes"),
            vcr_fixture_dir=_env_str("VCR_FIXTURE_DIR", "tests/fixtures/vcr"),
            # Trace
            trace_enabled=_env_bool("TRACE_ENABLED", True),
            trace_dir=_env_str("TRACE_DIR", "memory/traces"),
            trace_sanitize=_env_bool("TRACE_SANITIZE", True),
            # Tool Output
            tool_output_max_lines=_env_int("TOOL_OUTPUT_MAX_LINES", 2000),
            tool_output_max_bytes=_env_int("TOOL_OUTPUT_MAX_BYTES", 51200),
            tool_output_truncate_direction=_env_str("TOOL_OUTPUT_TRUNCATE_DIRECTION", "head"),
            tool_output_dir=_env_str("TOOL_OUTPUT_DIR", "tool-output"),
            tool_output_retention_days=_env_int("TOOL_OUTPUT_RETENTION_DAYS", 7),
            # Skills
            skills_refresh_on_call=_env_bool("SKILLS_REFRESH_ON_CALL", True),
            skills_prompt_char_budget=_env_int("SKILLS_PROMPT_CHAR_BUDGET", 12000),
            # Circuit Breaker
            circuit_failure_threshold=_env_int("CIRCUIT_FAILURE_THRESHOLD", 3),
            circuit_recovery_timeout=_env_int("CIRCUIT_RECOVERY_TIMEOUT", 300),
        )
```

### 3.6 废弃旧 /save /load

- `/save` `/load` 保留但输出 deprecation warning
- 引导用户使用 `/sessions` `/resume`
- 旧的 `save_session(path)` / `load_session(path)` 方法标记 `@deprecated`
- `_auto_save_session()` 已经是默认行为

### 3.7 工具耗时统计

```python
# _execute_step_tools 中
import time
for tool_call in tool_calls:
    start = time.time()
    observation = self._execute_tool(tool_name, tool_input)
    elapsed_ms = int((time.time() - start) * 1000)
    # 通过 trace_logger 记录
    trace_logger.log_event("tool_timing", {
        "tool": tool_name,
        "elapsed_ms": elapsed_ms,
    }, step=step)
```

TUI 层的 `ToolCallTree` 展示耗时：
```
Tools used this turn:
  Read (12ms)      src/main.py
  Grep (345ms)     class User
  Bash (2100ms)    pytest tests/
```

---

## 四、实施计划

### Phase 1: 基础设施（先做，不改变行为）

```
Step 1: 创建 core/features/base.py (AgentFeature 协议)
Step 2: 扩展 core/config.py (统一所有配置项)
Step 3: 创建 _env_str/_env_bool/_env_int/_env_float 辅助函数
Step 4: 更新 .env.example (自动生成脚本或手动同步)
Step 5: 全量测试验证（行为不应改变）
```

### Phase 2: 功能迁移（逐个迁移，每次验证）

```
Step 6: 迁移 WorktreeFeature (最简单，无运行时交互)
Step 7: 迁移 PlanModeFeature (runtime_blocks + 工具过滤)
Step 8: 迁移 BackgroundTaskFeature (runtime_blocks)
Step 9: 迁移 OutputStyleFeature (post_init + context_builder)
Step 10: 迁移 VCRFeature (llm_intercept)
Step 11: 迁移 HookFeature (runtime_blocks + pre/post_tool_use)
Step 12: 迁移 SessionFeature (auto-save)
Step 13: 迁移 AgentTeamsFeature (最复杂)
Step 14: 迁移 DelegateModeFeature (工具过滤)
```

### Phase 3: ReAct 循环重构

```
Step 15: 实现 _collect_runtime_blocks()
Step 16: 实现 _invoke_llm_with_interception()
Step 17: 重构 _execute_tool() 使用 Feature 拦截
Step 18: 添加工具耗时统计
Step 19: 废弃旧 /save /load API
```

---

## 五、文件清单

| 文件 | 操作 | 说明 |
|------|------|------|
| `core/features/__init__.py` | **新建** | Feature 注册表 |
| `core/features/base.py` | **新建** | AgentFeature 协议 (~60 行) |
| `core/features/worktree.py` | **新建** | WorktreeFeature (~40 行) |
| `core/features/plan_mode.py` | **新建** | PlanModeFeature (~50 行) |
| `core/features/background_task.py` | **新建** | BackgroundTaskFeature (~30 行) |
| `core/features/output_style.py` | **新建** | OutputStyleFeature (~40 行) |
| `core/features/vcr.py` | **新建** | VCRFeature (~30 行) |
| `core/features/hook.py` | **新建** | HookFeature (~60 行) |
| `core/features/session.py` | **新建** | SessionFeature (~50 行) |
| `core/features/agent_teams.py` | **新建** | AgentTeamsFeature (~60 行) |
| `core/features/delegate.py` | **新建** | DelegateModeFeature (~30 行) |
| `core/config.py` | **重写** | 统一配置 (~200 行) |
| `agents/codeAgent.py` | **重构** | __init__ 瘦身 + ReAct 循环重构 |
| `.env.example` | **更新** | 完整配置模板 |
| `tests/test_features.py` | **新建** | Feature 协议测试 |
| `tests/test_config.py` | **新建** | 统一配置测试 |

### 预估

| Phase | 新增行数 | 修改行数 | 净效果 |
|-------|---------|---------|--------|
| Phase 1 | ~300 | ~200 | Config 扩张 |
| Phase 2 | ~450 | -150 | CodeAgent 瘦身 |
| Phase 3 | ~100 | -80 | ReAct 循环精简 |
| **总计** | **~850** | **-230** | CodeAgent 从 ~1500 行降到 ~1200 行 |

---

### 3.8 MCP 连接状态显示

**问题：** 当前 MCP 使用 `manual` 模式，后台静默连接，用户不知道哪些 server 可用。

**方案：**

```python
# tools/mcp/loader.py 新增
def get_mcp_status() -> list[dict]:
    """返回 MCP 服务器连接状态列表。"""
    return [
        {"name": "fetch", "status": "connected", "tools": 2},
        {"name": "context7", "status": "pending", "tools": 0, "error": "timeout"},
        {"name": "tavily-mcp", "status": "connected", "tools": 1},
    ]
```

**CLI 集成：**

```bash
# 启动时显示 MCP 状态
python scripts/chat_test_agent.py
  → MCP: fetch ✅ (2 tools), context7 ⏳ (pending), tavily ✅ (1 tool)

# /mcp 命令
/mcp → 表格显示所有 MCP server 状态
/mcp retry → 手动重试 pending 的 server
```

**Feature 实现：** 新增 `MCPFeature` (order=25)，负责连接状态跟踪和 `/mcp` 命令。

### 3.9 插件系统

**问题：** 当前 hooks/skills/output_styles 各自独立配置，用户需要在不同位置配置不同文件。

**方案：** Feature 协议天然是插件系统的基础。外部插件实现 `AgentFeature` 接口：

```
.mycode/
  plugins/
    my-plugin/
      plugin.json         ← {name, version, features: ["hooks", "skills", "styles"]}
      hooks.json          ← hook 定义
      skills/             ← 技能 Markdown 文件
      output_styles/      ← 输出风格 Markdown 文件
      feature.py          ← 自定义 AgentFeature 实现（可选）
```

**plugin.json 格式：**

```json
{
  "name": "my-plugin",
  "version": "1.0.0",
  "description": "Custom security audit plugin",
  "features": {
    "hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [...]}]},
    "skills": ["skills/"],
    "output_styles": ["styles/"]
  }
}
```

**加载机制：**

```python
# core/plugin_loader.py (新建)
class PluginLoader:
    def discover(self, project_root) -> list[AgentFeature]:
        """扫描 .mycode/plugins/ 目录，加载所有 plugin.json。"""
        for plugin_dir in self._list_plugin_dirs():
            manifest = self._load_manifest(plugin_dir)
            features = []
            if "hooks" in manifest.features:
                features.append(self._build_hook_feature(manifest))
            if "skills" in manifest.features:
                features.append(self._build_skill_feature(manifest))
            # ...
            return features
```

**与 Feature 协议的关系：** 插件加载的 Feature 和内置 Feature 使用相同的 `AgentFeature` 接口，CodeAgent 不需要区分内置和插件。`FEATURES` 列表 = 内置 Features + 插件 Features。

### 3.10 子代理流式输出

**问题：** 当前 Task 子代理跑完后一次性返回结果，用户看不到中间过程。

**方案：** 子代理每步写入进度文件，主代理 TUI 轮询展示。

**存储格式：** `.tasks/progress/{task_id}.jsonl`

```jsonl
{"step": 1, "type": "thought", "content": "Let me explore the auth module..."}
{"step": 1, "type": "action", "tool": "Grep", "input": {"pattern": "class.*Auth"}}
{"step": 1, "type": "observation", "summary": "Found 3 matches"}
{"step": 2, "type": "thought", "content": "Now I'll read the main auth file..."}
{"step": 2, "type": "action", "tool": "Read", "input": {"path": "src/auth.py"}}
```

**BackgroundTaskRunner 改造：**

```python
# core/background_task.py
class BackgroundTaskRunner:
    def launch(self, task_id, runner_callable, description):
        # 新建进度文件
        progress_path = self._progress_path(task_id)
        # ...
        thread = threading.Thread(
            target=self._run_with_progress,
            args=(task_id, runner_callable, progress_path),
            daemon=True,
        )

    def _run_with_progress(self, task_id, callable, progress_path):
        """运行子代理，每步写入进度。"""
        def progress_callback(step, event_type, data):
            with open(progress_path, "a") as f:
                f.write(json.dumps({"step": step, "type": event_type, **data}) + "\n")

        result = callable(progress_callback=progress_callback)
        self._write_result(task_id, result)

    def get_progress(self, task_id, since_step=0) -> list[dict]:
        """读取进度（从指定步骤之后）。"""
        # ...
```

**TUI 展示：** `StreamingResponse` 新增 `show_subagent_progress(task_id)` 方法，在子代理运行时实时展示其思考过程。

---

## 四、实施计划

### Phase 1: 基础设施（先做，不改变行为）

```
Step 1: 创建 core/features/base.py (AgentFeature 协议)
Step 2: 扩展 core/config.py (统一所有配置项)
Step 3: 创建 _env_str/_env_bool/_env_int/_env_float 辅助函数
Step 4: 更新 .env.example (自动生成脚本或手动同步)
Step 5: 全量测试验证（行为不应改变）
```

### Phase 2: 功能迁移（逐个迁移，每次验证）

```
Step 6: 迁移 WorktreeFeature (最简单，无运行时交互)
Step 7: 迁移 PlanModeFeature (runtime_blocks + 工具过滤)
Step 8: 迁移 BackgroundTaskFeature (runtime_blocks)
Step 9: 迁移 OutputStyleFeature (post_init + context_builder)
Step 10: 迁移 VCRFeature (llm_intercept)
Step 11: 迁移 HookFeature (runtime_blocks + pre/post_tool_use)
Step 12: 迁移 SessionFeature (auto-save)
Step 13: 迁移 AgentTeamsFeature (最复杂)
Step 14: 迁移 DelegateModeFeature (工具过滤)
Step 15: 新增 MCPFeature (连接状态 + /mcp 命令)
```

### Phase 3: ReAct 循环重构

```
Step 16: 实现 _collect_runtime_blocks()
Step 17: 实现 _invoke_llm_with_interception()
Step 18: 重构 _execute_tool() 使用 Feature 拦截
Step 19: 添加工具耗时统计
Step 20: 废弃旧 /save /load API
```

### Phase 4: 插件系统

```
Step 21: 创建 core/plugin_loader.py (插件发现 + 加载)
Step 22: 实现 .mycode/plugins/ 目录扫描
Step 23: 插件 Feature 与内置 Feature 统一注册
Step 24: 插件测试
```

### Phase 5: 子代理流式

```
Step 25: BackgroundTaskRunner 增加进度回调
Step 26: TUI 层增加子代理进度展示
Step 27: TaskOutput 工具支持流式查询
Step 28: 子代理流式测试
```

### Phase 6: 体验优化（独立设计文档）

```
Step 29: MCP 连接状态 (见 Section 3.8)
Step 30: 首次启动向导 (见 docs/plans/2026-06-22-first-run-wizard-design.md)
Step 31: Token 用量预算 (见 docs/plans/2026-06-22-token-budget-design.md)
```

---

## 五、文件清单

| 文件 | 操作 | 说明 |
|------|------|------|
| `core/features/__init__.py` | **新建** | Feature 注册表 |
| `core/features/base.py` | **新建** | AgentFeature 协议 (~80 行) |
| `core/features/worktree.py` | **新建** | WorktreeFeature (~40 行) |
| `core/features/plan_mode.py` | **新建** | PlanModeFeature (~50 行) |
| `core/features/background_task.py` | **新建** | BackgroundTaskFeature (~30 行) |
| `core/features/output_style.py` | **新建** | OutputStyleFeature (~40 行) |
| `core/features/vcr.py` | **新建** | VCRFeature (~30 行) |
| `core/features/hook.py` | **新建** | HookFeature (~60 行) |
| `core/features/session.py` | **新建** | SessionFeature (~50 行) |
| `core/features/agent_teams.py` | **新建** | AgentTeamsFeature (~60 行) |
| `core/features/delegate.py` | **新建** | DelegateModeFeature (~30 行) |
| `core/features/mcp.py` | **新建** | MCPFeature (~60 行) |
| `core/plugin_loader.py` | **新建** | 插件发现 + 加载 (~200 行) |
| `core/config.py` | **重写** | 统一配置 (~250 行) |
| `core/background_task.py` | **修改** | 流式进度回调 (+80 行) |
| `agents/codeAgent.py` | **重构** | __init__ 瘦身 + ReAct 重构 |
| `tui/streaming.py` | **修改** | 子代理进度展示 (+50 行) |
| `.env.example` | **更新** | 完整配置模板 |
| `tests/test_features.py` | **新建** | Feature 协议测试 |
| `tests/test_config.py` | **新建** | 统一配置测试 |
| `tests/test_plugin_loader.py` | **新建** | 插件加载测试 |
| `tests/test_subagent_streaming.py` | **新建** | 子代理流式测试 |

### 预估

| Phase | 新增行数 | 修改行数 | 净效果 |
|-------|---------|---------|--------|
| Phase 1 | ~350 | ~250 | Config 扩张 |
| Phase 2 | ~550 | -200 | CodeAgent 瘦身 |
| Phase 3 | ~120 | -100 | ReAct 循环精简 |
| Phase 4 | ~250 | ~30 | 插件系统 |
| Phase 5 | ~180 | ~80 | 子代理流式 |
| **总计** | **~1450** | **-300** | CodeAgent ~1200 行 |

---

## 六、不做的事情（v1 范围外）

- **不拆 CodeAgent 为多个类** — 保持单文件，Feature 协议已足够解耦
- **不做 DI 容器** — 依赖简单，手工注入够用
- **不改 Feature 的公共 API** — 只影响 CodeAgent 内部，chat_test_agent.py 不受影响
