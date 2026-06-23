"""ToolBootstrap — 工具自动发现与依赖注入。

扫描 tools/builtin/ 目录，自动发现所有 Tool 子类，
根据构造函数参数名自动注入依赖，统一注册到 ToolRegistry。

使用方式:
    bootstrap = ToolBootstrap(registry=tool_registry, project_root=project_root)
    bootstrap.provide("code_agent", self)
    bootstrap.provide("team_manager", self.team_manager)
    bootstrap.provide("background_runner", self._background_runner)
    bootstrap.provide("skill_loader", self._skill_loader)
    bootstrap.provide("main_llm", self.llm)
    bootstrap.provide("interactive", self.interactive)
    bootstrap.discover_and_register()
"""

from __future__ import annotations

import importlib
import inspect
import logging
import pkgutil
from pathlib import Path
from typing import Any

from tools.base import Tool
from tools.registry import ToolRegistry

logger = logging.getLogger(__name__)

# 团队工具的模块路径列表 — 仅在 enable_agent_teams 时单独注册
# discover_and_register() 会跳过这些模块，避免 team_manager=None 时的 ValueError 噪音
_TEAM_TOOL_MODULES = frozenset({
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
})


class ToolBootstrap:
    """工具自动发现与注册。

    通过 DI 容器模式，扫描 tools/builtin/ 目录下的所有 Tool 子类，
    根据构造函数参数名自动注入依赖，统一注册到 ToolRegistry。

    新增工具只需：
    1. 继承 Tool，放在 tools/builtin/ 下
    2. 构造函数参数名与 provide() 注册的名称匹配
    3. 无需修改 CodeAgent
    """

    def __init__(self, registry: ToolRegistry, project_root: str):
        self._registry = registry
        self._project_root = project_root
        self._providers: dict[str, Any] = {
            "project_root": project_root,
            "working_dir": project_root,
        }

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def provide(self, name: str, value: Any) -> None:
        """注册依赖提供者。

        工具构造函数中同名的参数将自动注入此值。
        典型用法:
            bootstrap.provide("code_agent", self)
            bootstrap.provide("team_manager", self.team_manager)
        """
        self._providers[name] = value

    def discover_and_register(self) -> list[str]:
        """扫描 tools/builtin/ 目录，自动发现并注册所有非团队 Tool 子类。

        团队工具（TeamCreate/SendMessage 等 15 个）会被跳过，
        应通过 register_team_tools() 单独注册。

        Returns:
            注册成功的工具名称列表。
        """
        import tools.builtin as builtin_pkg

        registered: list[str] = []
        package_path = Path(builtin_pkg.__path__[0])

        for module_info in pkgutil.iter_modules([str(package_path)]):
            module_full_name = f"tools.builtin.{module_info.name}"

            # 跳过团队工具模块
            if module_full_name in _TEAM_TOOL_MODULES:
                continue

            try:
                module = importlib.import_module(module_full_name)
            except Exception as exc:
                logger.warning("Failed to import %s: %s", module_full_name, exc)
                continue

            for _name, obj in inspect.getmembers(module, inspect.isclass):
                if not issubclass(obj, Tool) or obj is Tool:
                    continue
                # 只注册定义在当前模块中的类（跳过 re-export）
                if obj.__module__ != module_full_name:
                    continue

                try:
                    tool_instance = self._instantiate(obj)
                    self._registry.register_tool(tool_instance)
                    registered.append(tool_instance.name)
                    logger.debug("Auto-registered tool: %s", tool_instance.name)
                except Exception as exc:
                    logger.warning(
                        "Failed to auto-register %s from %s: %s",
                        obj.__name__, module_full_name, exc,
                    )

        return registered

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _instantiate(self, tool_cls: type[Tool]) -> Tool:
        """根据工具类的 __init__ 签名自动注入依赖。

        策略:
        1. 遍历 __init__ 的每个参数
        2. 若参数名在 _providers 中，注入对应值
        3. 若有默认值，使用默认值
        4. project_root 始终强制注入
        """
        sig = inspect.signature(tool_cls.__init__)
        kwargs: dict[str, Any] = {}

        for param_name, param in sig.parameters.items():
            if param_name == "self":
                continue

            if param_name in self._providers:
                kwargs[param_name] = self._providers[param_name]
            elif param_name == "project_root":
                kwargs[param_name] = self._project_root
            elif param_name == "working_dir":
                kwargs[param_name] = self._project_root
            # else: 参数不在 providers 中且有默认值 → 跳过，让工具使用自己的默认值

        return tool_cls(**kwargs)


# ---------------------------------------------------------------------------
# 团队工具注册（独立函数）
# ---------------------------------------------------------------------------

def register_team_tools(bootstrap: ToolBootstrap) -> list[str]:
    """注册 AgentTeams 系列工具。

    仅在 enable_agent_teams=True 且 team_manager 可用时调用。
    通过 bootstrap._instantiate() 利用已注册的 providers 自动注入依赖。

    Args:
        bootstrap: 已配置好 team_manager provider 的 ToolBootstrap 实例。

    Returns:
        注册成功的工具名称列表。
    """
    registered: list[str] = []

    for module_path in sorted(_TEAM_TOOL_MODULES):
        try:
            module = importlib.import_module(module_path)
        except Exception as exc:
            logger.warning("Failed to import team tool module %s: %s", module_path, exc)
            continue

        for _name, obj in inspect.getmembers(module, inspect.isclass):
            if not issubclass(obj, Tool) or obj is Tool:
                continue
            if obj.__module__ != module_path:
                continue

            try:
                tool_instance = bootstrap._instantiate(obj)
                bootstrap._registry.register_tool(tool_instance)
                registered.append(tool_instance.name)
                logger.debug("Registered team tool: %s", tool_instance.name)
            except Exception as exc:
                logger.warning(
                    "Failed to register team tool %s from %s: %s",
                    obj.__name__, module_path, exc,
                )
            break  # 每个模块只取第一个 Tool 子类

    return registered
