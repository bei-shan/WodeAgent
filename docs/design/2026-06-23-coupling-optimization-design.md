# 耦合度优化设计文档

> 日期: 2026-06-23 | 优先级: P1-P2 | 范围: 工具自动发现 + 统一配置 + CodeAgent 拆分
> 前置: 2026-06-22-codeagent-architecture-refactor.md (Phase 1-6 已完成)

---

## 一、问题诊断

### 1.1 CodeAgent 直接依赖 33 个具体工具类

当前 `codeAgent.py` 有 **46 个 import**，其中 18 个是顶层 import 工具类，15 个在方法内延迟 import：

```python
# 顶层 import — 18 个工具类
from tools.builtin.list_files import ListFilesTool
from tools.builtin.search_files_by_name import SearchFilesByNameTool
from tools.builtin.search_code import GrepTool
from tools.builtin.read_file import ReadTool
from tools.builtin.write_file import WriteTool
from tools.builtin.edit_file import EditTool
from tools.builtin.edit_file_multi import MultiEditTool
from tools.builtin.todo_write import TodoWriteTool
from tools.builtin.skill import SkillTool
from tools.builtin.bash import BashTool
from tools.builtin.ask_user import AskUserTool
from tools.builtin.task import TaskTool
from tools.builtin.enter_worktree import EnterWorktreeTool
from tools.builtin.exit_worktree import ExitWorktreeTool
from tools.builtin.enter_plan_mode import EnterPlanModeTool
from tools.builtin.exit_plan_mode import ExitPlanModeTool
from tools.builtin.task_output import TaskOutputTool
from tools.builtin.switch_model import SwitchModelTool

# 延迟 import — 15 个 Team 工具类（在 _register_agent_teams_tools 内部）
from tools.builtin.team_create import TeamCreateTool
from tools.builtin.send_message import SendMessageTool
...
```

**问题：**
- 新增一个工具需要修改 `codeAgent.py` 的 2 处（import + register 调用）
- 46 个 import 让 CodeAgent 成为全项目耦合度最高的文件
- 每个工具的构造函数参数不同（有的需要 `team_manager`，有的需要 `code_agent`，有的需要 `background_runner`），注册逻辑散落且不一致

### 1.2 配置散落 — 55 处 os.getenv()

`os.getenv()` 直接调用分布在 **19 个文件**中：

| 文件 | 调用次数 | 示例配置项 |
|------|---------|-----------|
| `core/config.py` | 18 | CONTEXT_WINDOW, COMPRESSION_THRESHOLD, ... |
| `core/context_engine/observation_truncator.py` | 7 | TOOL_OUTPUT_MAX_LINES, TOOL_OUTPUT_MAX_BYTES, ... |
| `tools/builtin/task.py` | 7 | SUBAGENT_MAX_STEPS, LIGHT_LLM_MODEL_ID, ... |
| `core/vcr.py` | 3 | VCR_ENABLED, VCR_RECORD_MODE, VCR_FIXTURE_DIR |
| `core/model_profiles.py` | 5 | MODEL_PROFILES, MODEL_<NAME>_ID, MODEL_POINTER_* |
| `core/features/output_style.py` | 1 | AGENT_OUTPUT_STYLE |
| `core/features/worktree.py` | 2 | WORKTREE_STORE_DIR, WORKTREE_BASE_REF |
| `core/team_engine/execution.py` | 1 | TEAM_WORKER_MAX_STEPS |
| `core/team_engine/manager.py` | 1 | TEAM_LLM_MAX_CONCURRENCY |
| `core/team_engine/store.py` | 2 | TEAM_MAX_INBOX_SIZE, TEAM_MAX_WORK_ITEMS |
| `core/worktree/manager.py` | 1 | WORKTREE_STORE_DIR |
| `core/background_task.py` | 1 | BG_TASK_OUTPUT_DIR |
| `tools/registry.py` | 2 | CIRCUIT_FAILURE_THRESHOLD, CIRCUIT_RECOVERY_TIMEOUT |
| `core/llm.py` | 1 | (helper function `_env`) |
| `agents/codeAgent.py` | 3 | AGENT_INTERACTIVE, SKILLS_REFRESH_ON_CALL, SKILLS_PROMPT_CHAR_BUDGET |
| `core/team_engine/display_mode.py` | 1 | TMUX |
| `tools/builtin/skill.py` | 1 | (env var read in tool) |

**问题：**
- 新增配置没有统一入口，`.env.example` 靠手动同步
- Config 类只覆盖了 ~18 项，其余 ~37 处直接读环境变量
- 同一变量（如 `WORKTREE_STORE_DIR`）在 `core/features/worktree.py` 和 `core/worktree/manager.py` **两处**重复读取
- 无法做配置校验、文档生成、默认值统一管理

### 1.3 工具注册逻辑硬编码在 CodeAgent 中

`_register_builtin_tools()` 方法约 100 行，每个工具需要手动构造并传入不同的依赖：

```python
# 简单工具 — 只需 project_root
self.tool_registry.register_tool(ReadTool(project_root=self.project_root))

# 需要 skill_loader
self.tool_registry.register_tool(
    SkillTool(project_root=self.project_root, skill_loader=self._skill_loader)
)

# 需要 code_agent 自身引用
self.tool_registry.register_tool(
    EnterWorktreeTool(project_root=self.project_root, worktree_manager=self._worktree_manager, code_agent=self)
)

# 需要多个依赖
self.tool_registry.register_tool(
    TaskTool(project_root=self.project_root, main_llm=self.llm,
             tool_registry=self.tool_registry, team_manager=self.team_manager,
             background_runner=self._background_runner)
)
```

**问题：**
- 每个工具需要的依赖不同，注册代码无法复用
- 依赖关系隐含在 CodeAgent 的属性中，没有显式声明
- 无法单独测试工具注册逻辑

---

## 二、目标架构

### 2.1 工具自动发现

```
现状:
  CodeAgent → 33 个显式 import → 手动 register_tool()

目标:
  CodeAgent → ToolBootstrap.discover_and_register()
                ├── 扫描 tools/builtin/*.py
                ├── 识别继承 Tool 的类
                ├── 解析 __init__ 参数类型 → 自动注入依赖
                └── 调用 register_tool()
```

### 2.2 统一配置

```
现状:
  os.getenv("XXX") 散落在 19 个文件中

目标:
  Config.from_env() → 覆盖所有配置项（~55 项 → Config 统一管理）
  所有模块 → config.xxx (属性访问，不再直接 os.getenv)
```

### 2.3 CodeAgent 拆分

```
现状:
  CodeAgent.__init__ (80 行) + _init_core() + _init_tools() (100 行)
  → 约 180 行初始化代码

目标:
  CodeAgent.__init__ (30 行)
    ├── _init_core()         # 核心组件
    ├── _init_features()     # Feature 协议
    └── ToolBootstrap(agent)  # 工具注册独立类
          ├── .discover()     # 扫描 + 识别
          └── .register_all() # 统一注册
```

---

## 三、详细设计

### 3.1 工具自动发现 — ToolBootstrap

#### 3.1.1 核心机制：依赖注入容器

每个工具的 `__init__` 参数分两类：
- **框架注入**：`project_root`, `working_dir`, `permission_gate` — 由 Tool 基类管理
- **业务依赖**：`team_manager`, `code_agent`, `background_runner`, `skill_loader`, `main_llm`, `tool_registry` — 由 DI 容器提供

```python
# core/tool_bootstrap.py (新建)

import importlib
import inspect
import pkgutil
from pathlib import Path
from typing import Any, Callable

from tools.base import Tool
from tools.registry import ToolRegistry


class ToolBootstrap:
    """工具自动发现与注册。

    扫描 tools/builtin/ 目录，识别所有 Tool 子类，根据构造函数参数类型
    自动注入依赖，统一注册到 ToolRegistry。

    使用方式:
        bootstrap = ToolBootstrap(
            registry=tool_registry,
            project_root=project_root,
        )
        bootstrap.provide("team_manager", team_manager)
        bootstrap.provide("code_agent", self)
        bootstrap.provide("background_runner", self._background_runner)
        bootstrap.provide("skill_loader", self._skill_loader)
        bootstrap.provide("main_llm", self.llm)
        bootstrap.discover_and_register()
    """

    # 依赖注入的类型 → 提供者映射
    # key: 参数类型名（匹配 __init__ 参数名）
    # value: 实际注入的值
    _TYPE_REGISTRY: dict[str, Any] = {}

    def __init__(self, registry: ToolRegistry, project_root: str):
        self._registry = registry
        self._project_root = project_root
        self._providers: dict[str, Any] = {
            "project_root": project_root,
            "working_dir": project_root,
        }

    def provide(self, name: str, value: Any) -> None:
        """注册依赖提供者。工具构造函数中同名的参数将自动注入。"""
        self._providers[name] = value

    def discover_and_register(self) -> list[str]:
        """扫描 tools/builtin/ 目录，自动发现并注册所有 Tool 子类。

        Returns:
            注册成功的工具名称列表。
        """
        import tools.builtin as builtin_pkg

        registered: list[str] = []
        package_path = Path(builtin_pkg.__path__[0])

        for module_info in pkgutil.iter_modules([str(package_path)]):
            module = importlib.import_module(f"tools.builtin.{module_info.name}")

            for name, obj in inspect.getmembers(module, inspect.isclass):
                if not issubclass(obj, Tool) or obj is Tool:
                    continue

                # 跳过需要特殊处理的工具（通过标记控制）
                if getattr(obj, "_skip_auto_register", False):
                    continue

                try:
                    tool_instance = self._instantiate(obj)
                    self._registry.register_tool(tool_instance)
                    registered.append(tool_instance.name)
                except Exception as exc:
                    # 记录警告但不中断整个注册流程
                    import logging
                    logging.getLogger(__name__).warning(
                        "Failed to auto-register tool %s: %s", obj.__name__, exc
                    )

        return registered

    def _instantiate(self, tool_cls: type[Tool]) -> Tool:
        """根据工具类的 __init__ 签名自动注入依赖。

        策略:
        1. 检查 __init__ 的参数名
        2. 对每个参数，在 _providers 中查找同名提供者
        3. 找到则注入，未找到则使用默认值（如果有）或跳过
        4. 强制注入 project_root
        """
        sig = inspect.signature(tool_cls.__init__)
        kwargs: dict[str, Any] = {}

        for param_name, param in sig.parameters.items():
            if param_name == "self":
                continue

            if param_name in self._providers:
                kwargs[param_name] = self._providers[param_name]
            elif param.default is not inspect.Parameter.empty:
                # 有默认值的参数跳过，使用类自身默认值
                pass
            elif param_name == "project_root":
                kwargs[param_name] = self._project_root
            elif param_name == "working_dir":
                kwargs[param_name] = self._project_root

        return tool_cls(**kwargs)


# ---------------------------------------------------------------------------
# 团队工具的延迟注册（通过 provide 注入 team_manager 后单独调用）
# ---------------------------------------------------------------------------

TEAM_TOOL_MODULES = [
    "tools.builtin.team_create",
    "tools.builtin.send_message",
    "tools.builtin.team_status",
    "tools.builtin.team_delete",
    "tools.builtin.team_cleanup",
    "tools.builtin.team_approvals",
    "tools.builtin.team_approve_plan",
    "tools.builtin.team_fanout",
    "tools.builtin.team_collect",
    "tools.builtin.team_task_create",
    "tools.builtin.team_task_get",
    "tools.builtin.team_task_update",
    "tools.builtin.team_task_list",
    "tools.builtin.team_list",
    "tools.builtin.team_retry",
]


def register_team_tools(bootstrap: ToolBootstrap) -> list[str]:
    """注册 AgentTeams 系列工具（仅在 enable_agent_teams=True 时调用）。

    团队工具需要 team_manager 依赖，该依赖通过 bootstrap.provide() 注入。
    """
    registered: list[str] = []
    import logging
    logger = logging.getLogger(__name__)

    for module_path in TEAM_TOOL_MODULES:
        try:
            module = importlib.import_module(module_path)
            # 找到模块中唯一的 Tool 子类
            for name, obj in inspect.getmembers(module, inspect.isclass):
                if issubclass(obj, Tool) and obj is not Tool and obj.__module__ == module_path:
                    tool_instance = bootstrap._instantiate(obj)
                    bootstrap._registry.register_tool(tool_instance)
                    registered.append(tool_instance.name)
                    break
        except Exception as exc:
            logger.warning("Failed to register team tool from %s: %s", module_path, exc)

    return registered
```

#### 3.1.2 CodeAgent 集成方式

```python
# agents/codeAgent.py — 新 _init_tools 方法

def _init_tools(self) -> None:
    """Register built-in and MCP tools via ToolBootstrap auto-discovery."""
    from core.tool_bootstrap import ToolBootstrap, register_team_tools

    bootstrap = ToolBootstrap(
        registry=self.tool_registry,
        project_root=self.project_root,
    )

    # 注册业务依赖
    bootstrap.provide("code_agent", self)
    bootstrap.provide("team_manager", self.team_manager)
    bootstrap.provide("background_runner", self._background_runner)
    bootstrap.provide("skill_loader", self._skill_loader)
    bootstrap.provide("main_llm", self.llm)
    bootstrap.provide("interactive", self.interactive)

    # 自动发现并注册所有内置工具
    bootstrap.discover_and_register()

    # 团队工具（仅在启用时注册）
    if self.enable_agent_teams and self.team_manager:
        register_team_tools(bootstrap)

    # MCP 工具
    self._register_mcp_tools()
    self.context_builder.set_mcp_tools_prompt(self._mcp_tools_prompt)
```

#### 3.1.3 工具类需要的调整

每个工具类的构造函数参数名必须与 DI 容器的 provider name 精确匹配：

```python
# 示例：现有工具构造函数已经符合约定，无需修改
class EnterWorktreeTool(Tool):
    def __init__(self, project_root, working_dir=None,
                 worktree_manager=None, code_agent=None):
        # project_root     → bootstrap 自动注入
        # worktree_manager → bootstrap.provide("worktree_manager", ...)
        # code_agent       → bootstrap.provide("code_agent", self)
        ...

class TaskTool(Tool):
    def __init__(self, project_root, working_dir=None,
                 main_llm=None, tool_registry=None,
                 team_manager=None, background_runner=None):
        # main_llm           → bootstrap.provide("main_llm", self.llm)
        # tool_registry      → bootstrap.provide("tool_registry", ...)
        # team_manager       → bootstrap.provide("team_manager", ...)
        # background_runner  → bootstrap.provide("background_runner", ...)
        ...
```

**需要统一参数名的情况（少数）：**

| 当前参数名 | 应改为 | 所在工具 |
|-----------|--------|---------|
| `llm` | `main_llm` | TaskTool.__init__ |
| `interactive` (类属性) | `interactive` (构造函数参数) | AskUserTool |

这些改动很小，且向后兼容（通过默认值）。

---

### 3.2 统一配置 — Config 扩展

#### 3.2.1 目标：所有 os.getenv() → Config 属性

将当前 55 处散落的 `os.getenv()` 全部收敛到 `Config` 类，通过 `config.xxx` 属性访问。

```python
# core/config.py — 扩展现有 Config 类

class Config(BaseModel):
    """统一配置管理。所有环境变量读取集中在此类。

    使用方式:
        config = Config.from_env()
        # 访问: config.tool_output_max_lines, config.vcr_enabled, ...
    """

    # ===== LLM (已有) =====
    default_model: str = "gpt-3.5-turbo"
    default_provider: str = "openai"
    temperature: float = 0.7
    max_tokens: int | None = None

    # ===== System (已有) =====
    debug: bool = False
    log_level: str = "INFO"
    show_react_steps: bool = True
    show_progress: bool = True

    # ===== Agent (已有) =====
    max_history_length: int = 100
    agent_interactive: bool = True

    # ===== AgentTeams (已有) =====
    enable_agent_teams: bool = False
    agent_teams_store_dir: str = ".teams"
    agent_tasks_store_dir: str = ".tasks"
    teammate_mode: str = "auto"
    delegate_mode: bool = False

    # ===== Context (已有) =====
    context_window: int = 128000
    compression_threshold: float = 0.8
    min_retain_rounds: int = 10
    summary_timeout: int = 120
    tool_message_format: str = "strict"

    # ===== Tool Output Truncation (新增) =====
    tool_output_max_lines: int = 2000
    tool_output_max_bytes: int = 51200
    tool_output_truncate_direction: str = "head"
    tool_output_head_tail_lines: int = 40
    tool_output_dir: str = "tool-output"
    tool_output_retention_days: int = 7

    # ===== Subagent (新增) =====
    subagent_max_steps: int = 15
    light_llm_model_id: str = ""
    light_llm_api_key: str = ""
    light_llm_base_url: str = ""
    light_llm_provider: str = "auto"
    light_llm_temperature: float = 0.5

    # ===== Worktree (新增) =====
    worktree_store_dir: str = ".worktrees"
    worktree_base_ref: str = "fresh"

    # ===== VCR (新增) =====
    vcr_enabled: bool = False
    vcr_record_mode: str = "new_episodes"
    vcr_fixture_dir: str = "tests/fixtures/vcr"

    # ===== Output Style (新增) =====
    output_style: str = "default"

    # ===== Trace (新增) =====
    trace_enabled: bool = True
    trace_dir: str = "memory/traces"
    trace_sanitize: bool = True

    # ===== MCP (新增) =====
    mcp_connect_mode: str = "manual"

    # ===== Circuit Breaker (新增) =====
    circuit_failure_threshold: int = 3
    circuit_recovery_timeout: int = 300

    # ===== Skills (新增) =====
    skills_refresh_on_call: bool = True
    skills_prompt_char_budget: int = 12000

    # ===== Background Task (新增) =====
    bg_task_output_dir: str = ".tasks/output"

    # ===== Team Advanced (新增) =====
    team_worker_max_steps: int = 8
    team_llm_max_concurrency: int = 4
    team_max_inbox_size: int = 10000
    team_max_work_items: int = 5000

    # ===== Model Profiles (不在此处管理，保持独立的 ModelProfileManager) =====

    @classmethod
    def from_env(cls) -> "Config":
        """从环境变量加载所有配置。"""
        from core.env_helpers import (
            _env_str, _env_bool, _env_int, _env_float, _env_int_optional
        )

        # AgentTeams 开关兼容 Claude Code 环境变量
        enable_teams_raw = _env_str("ENABLE_AGENT_TEAMS", "")
        if not enable_teams_raw:
            enable_teams_raw = _env_str("CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS", "false")

        teammate_mode_raw = (_env_str("TEAMMATE_MODE", "auto") or "auto").strip().lower()
        if teammate_mode_raw not in {"auto", "in-process", "tmux"}:
            teammate_mode_raw = "auto"

        return cls(
            # LLM
            default_model=_env_str("LLM_MODEL_ID", "gpt-3.5-turbo"),
            default_provider=_env_str("LLM_PROVIDER", "openai"),
            temperature=_env_float("TEMPERATURE", 0.7),
            max_tokens=_env_int_optional("MAX_TOKENS"),
            # System
            debug=_env_bool("DEBUG", False),
            log_level=_env_str("LOG_LEVEL", "INFO"),
            show_react_steps=_env_bool("SHOW_REACT_STEPS", True),
            show_progress=_env_bool("SHOW_PROGRESS", True),
            # Agent
            agent_interactive=_env_bool("AGENT_INTERACTIVE", True),
            # AgentTeams
            enable_agent_teams=enable_teams_raw.lower() in {"1", "true", "yes", "y", "on"},
            agent_teams_store_dir=_env_str("AGENT_TEAMS_STORE_DIR", ".teams"),
            agent_tasks_store_dir=_env_str("AGENT_TASKS_STORE_DIR", ".tasks"),
            teammate_mode=teammate_mode_raw,
            delegate_mode=_env_bool("TEAM_DELEGATE_MODE", False),
            # Context
            context_window=_env_int("CONTEXT_WINDOW", 128000),
            compression_threshold=_env_float("COMPRESSION_THRESHOLD", 0.8),
            min_retain_rounds=_env_int("MIN_RETAIN_ROUNDS", 10),
            summary_timeout=_env_int("SUMMARY_TIMEOUT", 120),
            # Tool Output
            tool_output_max_lines=_env_int("TOOL_OUTPUT_MAX_LINES", 2000),
            tool_output_max_bytes=_env_int("TOOL_OUTPUT_MAX_BYTES", 51200),
            tool_output_truncate_direction=_env_str("TOOL_OUTPUT_TRUNCATE_DIRECTION", "head"),
            tool_output_head_tail_lines=_env_int("TOOL_OUTPUT_HEAD_TAIL_LINES", 40),
            tool_output_dir=_env_str("TOOL_OUTPUT_DIR", "tool-output"),
            tool_output_retention_days=_env_int("TOOL_OUTPUT_RETENTION_DAYS", 7),
            # Subagent
            subagent_max_steps=_env_int("SUBAGENT_MAX_STEPS", 15),
            light_llm_model_id=_env_str("LIGHT_LLM_MODEL_ID", ""),
            light_llm_api_key=_env_str("LIGHT_LLM_API_KEY", ""),
            light_llm_base_url=_env_str("LIGHT_LLM_BASE_URL", ""),
            light_llm_provider=_env_str("LIGHT_LLM_PROVIDER", "auto"),
            light_llm_temperature=_env_float("LIGHT_LLM_TEMPERATURE", 0.5),
            # Worktree
            worktree_store_dir=_env_str("WORKTREE_STORE_DIR", ".worktrees"),
            worktree_base_ref=_env_str("WORKTREE_BASE_REF", "fresh"),
            # VCR
            vcr_enabled=_env_bool("VCR_ENABLED", False),
            vcr_record_mode=_env_str("VCR_RECORD_MODE", "new_episodes"),
            vcr_fixture_dir=_env_str("VCR_FIXTURE_DIR", "tests/fixtures/vcr"),
            # Output Style
            output_style=_env_str("AGENT_OUTPUT_STYLE", "default"),
            # Trace
            trace_enabled=_env_bool("TRACE_ENABLED", True),
            trace_dir=_env_str("TRACE_DIR", "memory/traces"),
            trace_sanitize=_env_bool("TRACE_SANITIZE", True),
            # MCP
            mcp_connect_mode=_env_str("MCP_CONNECT_MODE", "manual"),
            # Circuit Breaker
            circuit_failure_threshold=_env_int("CIRCUIT_FAILURE_THRESHOLD", 3),
            circuit_recovery_timeout=_env_int("CIRCUIT_RECOVERY_TIMEOUT", 300),
            # Skills
            skills_refresh_on_call=_env_bool("SKILLS_REFRESH_ON_CALL", True),
            skills_prompt_char_budget=_env_int("SKILLS_PROMPT_CHAR_BUDGET", 12000),
            # Background Task
            bg_task_output_dir=_env_str("BG_TASK_OUTPUT_DIR", ".tasks/output"),
            # Team Advanced
            team_worker_max_steps=_env_int("TEAM_WORKER_MAX_STEPS", 8),
            team_llm_max_concurrency=_env_int("TEAM_LLM_MAX_CONCURRENCY", 4),
            team_max_inbox_size=_env_int("TEAM_MAX_INBOX_SIZE", 10000),
            team_max_work_items=_env_int("TEAM_MAX_WORK_ITEMS", 5000),
        )
```

#### 3.2.2 Config 传递策略

不采用全局单例，而是通过依赖注入层层传递：

```
chat_test_agent.py
  → config = Config.from_env()
  → CodeAgent(..., config=config)
    → ToolBootstrap(config=config)
    → VCR.create(config=config)
    → TeamManager(..., config=config)
    → ObservationTruncator(config=config)
```

各模块从 `os.getenv()` 改为接收 `config` 参数：

```python
# 之前
class ObservationTruncator:
    def _get_max_lines(self):
        return int(os.getenv("TOOL_OUTPUT_MAX_LINES", "2000"))

# 之后
class ObservationTruncator:
    def __init__(self, config: Config):
        self._config = config

    def _get_max_lines(self):
        return self._config.tool_output_max_lines
```

#### 3.2.3 向后兼容策略

对于被多处引用的底层模块，采用渐进式迁移：

```python
# core/context_engine/observation_truncator.py

# 阶段 1：同时支持 config 注入和 os.getenv() fallback
class ObservationTruncator:
    def __init__(self, config: Config | None = None):
        self._config = config

    def _get_max_lines(self) -> int:
        if self._config:
            return self._config.tool_output_max_lines
        return int(os.getenv("TOOL_OUTPUT_MAX_LINES", "2000"))  # 兼容旧路径
```

---

### 3.3 文件变更清单

| 文件 | 操作 | 说明 |
|------|------|------|
| `core/tool_bootstrap.py` | **新建** | ToolBootstrap + DI 容器 (~200 行) |
| `core/config.py` | **重写** | 扩展 Config 覆盖所有 55 项配置 (~280 行) |
| `core/env_helpers.py` | **修改** | 新增 `_env_int_optional` 辅助函数 (+5 行) |
| `agents/codeAgent.py` | **重构** | 用 ToolBootstrap 替换 33 个 import + 100 行注册代码 (-80 行) |
| `core/context_engine/observation_truncator.py` | **修改** | os.getenv() → config 属性 (7 处) |
| `core/vcr.py` | **修改** | os.getenv() → config 属性 (3 处) |
| `core/features/output_style.py` | **修改** | os.getenv() → config 属性 (1 处) |
| `core/features/worktree.py` | **修改** | os.getenv() → config 属性 (2 处) |
| `core/team_engine/execution.py` | **修改** | os.getenv() → config 属性 (1 处) |
| `core/team_engine/manager.py` | **修改** | os.getenv() → config 属性 (1 处) |
| `core/team_engine/store.py` | **修改** | os.getenv() → config 属性 (2 处) |
| `core/worktree/manager.py` | **修改** | os.getenv() → config 属性 (1 处) |
| `core/background_task.py` | **修改** | os.getenv() → config 属性 (1 处) |
| `tools/registry.py` | **修改** | os.getenv() → config 属性 (2 处) |
| `tools/builtin/task.py` | **修改** | os.getenv() → config 属性 (7 处) |
| `tools/builtin/skill.py` | **修改** | os.getenv() → config 属性 (1 处) |
| `tools/builtin/ask_user.py` | **修改** | 统一 interactive 参数传递方式 (+3 行) |
| `tests/test_tool_bootstrap.py` | **新建** | ToolBootstrap 测试 (~120 行) |
| `tests/test_config.py` | **新建** | 统一配置测试 (~100 行) |

### 3.4 行数预估

| 模块 | 新增 | 修改 | 净效果 |
|------|------|------|--------|
| ToolBootstrap | +200 | — | +200 |
| Config 扩展 | +150 | -30 | +120 |
| CodeAgent | — | -80 | -80 |
| 各模块 os.getenv() → config | — | ~25 处改动 | ~0 |
| 测试 | +220 | — | +220 |
| **总计** | **~570** | **-110** | **+460** |

---

## 四、实施计划

### Phase 1: 统一配置（基础设施，不改变行为）

```
Step 1: 扩展 core/config.py，覆盖所有 55 项配置
Step 2: 添加 _env_int_optional 辅助函数到 core/env_helpers.py
Step 3: 更新 .env.example 为完整配置模板
Step 4: 编写 test_config.py 验证所有配置项
Step 5: 全量测试（行为不应改变）
```

### Phase 2: 工具自动发现

```
Step 6: 创建 core/tool_bootstrap.py (ToolBootstrap + DI 容器)
Step 7: 统一 TaskTool 和 AskUserTool 的参数名
Step 8: 重构 agents/codeAgent.py 的 _init_tools() 使用 ToolBootstrap
Step 9: 移除 codeAgent.py 中的 33 个工具 import
Step 10: 编写 test_tool_bootstrap.py
Step 11: 全量测试（所有工具注册行为不变）
```

### Phase 3: 配置迁移（逐个模块，每次验证）

```
Step 12: 迁移 observation_truncator.py (7 处)
Step 13: 迁移 vcr.py (3 处)
Step 14: 迁移 task.py (7 处)
Step 15: 迁移 registry.py (2 处)
Step 16: 迁移 features/* (3 处)
Step 17: 迁移 team_engine/* (4 处)
Step 18: 迁移 worktree/manager.py (1 处)
Step 19: 迁移 background_task.py (1 处)
Step 20: 迁移 skill.py (1 处)
Step 21: 迁移 codeAgent.py 中剩余 os.getenv() (3 处)
Step 22: 全量测试
```

---

## 五、验收标准

### 5.1 工具自动发现

- [ ] 新增一个工具类（继承 Tool）后，无需修改 CodeAgent 即可自动注册
- [ ] 33 个显式 import 从 codeAgent.py 中移除
- [ ] 所有现有工具测试通过
- [ ] DI 注入失败时有明确警告日志，不中断其他工具注册

### 5.2 统一配置

- [ ] Config 类覆盖所有 55 项配置
- [ ] 非 Config 路径的 `os.getenv()` 降至 0（ModelProfileManager 除外）
- [ ] `.env.example` 包含所有配置项及说明
- [ ] `Config.from_env()` 可完整反序列化所有配置

### 5.3 CodeAgent 拆分

- [ ] `codeAgent.py` import 从 46 降至 < 30
- [ ] `_init_tools()` 从 ~100 行降至 ~20 行
- [ ] 工具注册逻辑可独立测试

---

## 六、不做的事情（范围外）

- **不引入重量级 DI 框架**（如 `dependency-injector`）— 手工 DI 容器已足够
- **不改变工具类的公共接口**— 只改构造函数参数名（保持向后兼容）
- **不重构 ModelProfileManager**— 其配置读取方式独立，改动范围太大
- **不做配置热重载**— 启动时一次性加载，session 级不变
- **不做配置校验的 JSON Schema 生成**— v1 只做基本类型校验
