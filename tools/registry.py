"""工具注册表 - HelloAgents原生工具系统

包含乐观锁自动注入机制，框架自动管理 Read 元信息缓存。
"""

import json
import copy
import time
import logging
import os
import threading
from typing import TYPE_CHECKING, Optional, Any, Callable, TypedDict

from .base import Tool, ToolStatus, ErrorCode, ToolParameter
from .circuit_breaker import CircuitBreaker
from .mcp.schema_validation import compile_mcp_input_schema
from core.env import load_env
from core.runtime.config_scope import active_config
from core.runtime.tool_contracts import validate_tool_input
from core.runtime.tool_snapshot import (
    ToolStepBinding, ToolStepSnapshot, encode_tool_schema, schema_fingerprint,
)
from core.runtime.tool_authorization import HostedToolAuthority

if TYPE_CHECKING:
    from core.config import Config

load_env()

# Module-level config (injected by CodeAgent, falls back to os.getenv)
_config: "Optional[Config]" = None


def set_registry_config(config: "Config") -> None:
    """注入统一配置对象。"""
    global _config
    _config = config

# 设置日志
logger = logging.getLogger(__name__)

# Legacy public names remain denied even when a custom adapter uses one. Actual
# built-in controls also declare worker_parent_only, so aliases cannot bypass
# the worker boundary.
_PARENT_ONLY_TOOLS = frozenset({
    "Task", "AskUser", "EnterPlanMode", "ExitPlanMode",
    "EnterWorktree", "ExitWorktree", "ToolSearch", "SendMessage",
    "TeamApprovePlan", "TeamCreate", "TeamDelete", "TeamCleanup",
    "TeamFanout", "TeamRetry",
})


class ReadMeta(TypedDict):
    """Read 操作的元信息（用于乐观锁自动注入）"""
    path_resolved: str        # 解析后的规范化路径（主键）
    file_mtime_ms: int        # 文件修改时间（毫秒）
    file_size_bytes: int      # 文件大小（字节）
    captured_at: float        # 缓存时间戳（用于调试/过期策略）


class ToolRegistry:
    """
    HelloAgents工具注册表

    提供工具的注册、管理和执行功能。
    支持两种工具注册方式：
    1. Tool对象注册（推荐）
    2. 函数直接注册（简便）
    
    包含乐观锁自动注入机制，自动管理 Read 元信息缓存。
    """

    def __init__(self, config: "Optional[Config]" = None):
        self._config = config or active_config() or _config
        # Structural changes and model schema capture share one short-lived lock.
        # Tool execution never holds it across external I/O.
        self._registration_lock = threading.RLock()
        self._tools: dict[str, Tool] = {}
        self._functions: dict[str, dict[str, Any]] = {}
        # Registration provenance belongs to the session registry, not plugins.
        self._tool_owners: dict[str, str] = {}
        # Optional session-owned deferred catalog; never stores executors.
        self._discovery_state = None
        self._discovery_guard: Callable[[str], bool] | None = None
        # Shared restrictions (e.g. planning) follow workers; role-specific
        # restrictions (e.g. the leader's delegate mode) apply only locally.
        self._shared_mode_guard: Callable[[str], bool] | None = None
        self._local_mode_guard: Callable[[str], bool] | None = None
        # When set, deny schema AND execution for all other tools, including
        # dynamically registered plugins or MCP functions.
        self._sandbox_allowed_tools: frozenset[str] | None = None
        # Only bound by a trusted hosted worker after sandbox restriction.
        self._hosted_authority: HostedToolAuthority | None = None
        self._derived_parent: ToolRegistry | None = None
        self._derived_bindings: dict[str, Tool] = {}
        self._derived_adapters: dict[str, Tool] = {}
        self._derived_schema_hashes: dict[str, str] = {}
        # Read 元信息缓存（用于乐观锁自动注入）
        # key: path_resolved 或原始 path
        self._read_cache: dict[str, ReadMeta] = {}
        if self._config is not None:
            ft, rt = self._config.circuit_failure_threshold, self._config.circuit_recovery_timeout
        else:
            ft = int(os.getenv("CIRCUIT_FAILURE_THRESHOLD", "3"))
            rt = int(os.getenv("CIRCUIT_RECOVERY_TIMEOUT", "300"))
        self._circuit_breaker = CircuitBreaker(
            failure_threshold=ft,
            recovery_timeout=rt,
        )


    def configure(self, config: "Config") -> None:
        """Bind circuit policy to the owning Agent's private config.

        Call before dispatch; clients must not share a mutable ToolRegistry
        across independently configured Agent sessions.
        """
        self._config = config
        self._circuit_breaker = CircuitBreaker(
            failure_threshold=config.circuit_failure_threshold,
            recovery_timeout=config.circuit_recovery_timeout,
        )

    def restrict_to_tools(self, allowed: set[str]) -> None:
        """Guard every tool dispatch; no host fallback in Docker mode."""
        if self._sandbox_allowed_tools is not None:
            raise PermissionError("Sandbox tool policy cannot be relaxed after activation")
        self._sandbox_allowed_tools = frozenset(allowed)

    def bind_hosted_authority(self, authority: HostedToolAuthority) -> None:
        """One-way bind: no rebinding to another tenant or permissive local mode."""
        if not isinstance(authority, HostedToolAuthority):
            raise TypeError("Verified hosted tool authority required")
        with self._registration_lock:
            if self._hosted_authority is not None:
                raise PermissionError("Hosted execution authority is already bound")
            if self._sandbox_allowed_tools != frozenset({"Bash"}):
                raise PermissionError("Hosted tool authority requires an isolated Bash-only registry")
            self._hosted_authority = authority

    def inherit_hosted_authority(self, parent: "ToolRegistry") -> None:
        """Subagent registries cannot drop the parent's tenant execution fence."""
        if not isinstance(parent, ToolRegistry):
            raise TypeError("Source must be a ToolRegistry")
        with parent._registration_lock:
            authority = parent._hosted_authority
        if authority is not None:
            self.restrict_to_tools({"Bash"})
            self.bind_hosted_authority(authority)

    def derive_worker_registry(
        self, *, allowed_names: set[str] | None = None,
        denied_names: set[str] | None = None, config: "Optional[Config]" = None,
    ) -> "ToolRegistry":
        """Bind a worker to independent adapters and the parent's live fences.

        Adapter copies share external clients/services but have private binding
        fields, notably a noninteractive permission gate. The parent identity
        seal prevents an old worker copy surviving registration replacement.
        """
        child = ToolRegistry(config=config or self._config)
        denied = set(denied_names or ()) | _PARENT_ONLY_TOOLS
        with self._registration_lock:
            child._derived_parent = self
            for name, original in self._tools.items():
                if (name in denied or getattr(type(original), "worker_parent_only", False)
                        or (allowed_names is not None and name not in allowed_names)):
                    continue
                try:
                    source_hash = schema_fingerprint(self._tool_schema_spec(original))
                except Exception:
                    logger.warning("Skipping worker tool %s with invalid schema", name)
                    continue
                bound = copy.copy(original)
                if bound is original:
                    raise TypeError(f"Cannot isolate worker adapter '{name}'")
                gate = getattr(original, "_permission_gate", None)
                if gate is not None:
                    subagent_gate = getattr(gate, "subagent_gate", None)
                    if not callable(subagent_gate):
                        raise TypeError(f"Cannot isolate permission gate for '{name}'")
                    bound._permission_gate = subagent_gate()
                child.register_tool(bound, owner=self._tool_owners[name])
                child._derived_bindings[name] = original
                child._derived_adapters[name] = bound
                child._derived_schema_hashes[name] = source_hash
            if self._sandbox_allowed_tools is not None:
                child.restrict_to_tools(set(self._sandbox_allowed_tools))
            if self._hosted_authority is not None:
                child.bind_hosted_authority(self._hosted_authority)
        return child

    def _source_binding_current(self, name: str) -> bool:
        parent = self._derived_parent
        if parent is None:
            return True
        with parent._registration_lock:
            current = (
                self._tools.get(name) is self._derived_adapters.get(name)
                and name in self._derived_adapters
                and parent._tools.get(name) is self._derived_bindings.get(name)
                and name in self._derived_bindings
                and parent._tool_allowed(name)
                and parent._circuit_breaker.is_available(name)
            )
            if not current:
                return False
            try:
                return (
                    schema_fingerprint(parent._tool_schema_spec(self._derived_bindings[name]))
                    == self._derived_schema_hashes[name]
                )
            except Exception:
                return False

    def _hosted_tool_allowed(self, name: str) -> bool:
        policy = self._hosted_authority
        return (policy is None or policy.allows(name)) and (
            self._derived_parent is None or self._derived_parent._hosted_tool_allowed(name)
        )

    def _tool_allowed(self, name: str) -> bool:
        return (
            (self._sandbox_allowed_tools is None or name in self._sandbox_allowed_tools)
            and self._hosted_tool_allowed(name)
            and self._source_binding_current(name)
        )

    def enable_deferred_discovery(
        self, state, *, can_execute: Callable[[str], bool] | None = None,
    ) -> None:
        """Install exactly one session's discovery policy at the execution boundary."""
        if self._discovery_state is not None:
            raise RuntimeError("Deferred discovery already configured for this registry")
        if self._sandbox_allowed_tools is not None:
            raise PermissionError("Deferred discovery is unavailable in isolated sandbox mode")
        if "ToolSearch" not in self._tools:
            raise ValueError("Register ToolSearch before enabling deferred discovery")
        self._discovery_state = state
        self._discovery_guard = can_execute

    def configure_mode_guards(
        self, *, shared: Callable[[str], bool], local: Callable[[str], bool],
    ) -> None:
        """Bind live Agent mode policy independently of deferred discovery."""
        self._shared_mode_guard = shared
        self._local_mode_guard = local

    def _discovery_mode_allowed(self, name: str, *, for_worker: bool = False) -> bool:
        if self._derived_parent is not None and not self._derived_parent._discovery_mode_allowed(
            name, for_worker=True,
        ):
            return False
        guards = (self._shared_mode_guard, self._discovery_guard)
        if not for_worker:
            guards += (self._local_mode_guard,)
        try:
            return all(guard is None or bool(guard(name)) for guard in guards)
        except Exception:
            # Mode and capability lookup are security-sensitive.
            return False

    def _discovery_visible(self, name: str) -> bool:
        if self._discovery_state is not None and not self._discovery_state.is_visible(name):
            return False
        return self._derived_parent is None or self._derived_parent._discovery_visible(name)

    def activate_discovered_tools(self) -> None:
        """Publish pending search results only when building the next model step.

        A ToolSearch call in a multi-call response must not make a guessed
        tool executable in that same response, before the model has seen its
        validated schema. Revoked or broken registrations are never activated.
        """
        state = self._discovery_state
        if state is None or not state.pending_names():
            return
        available = {
            spec["function"]["name"]
            for spec in self.get_openai_tools(include_deferred=True)
        }
        state.activate_pending(lambda name: name in available and self._discovery_mode_allowed(name))

    def register_tool(
        self, tool: Tool, *, owner: str = "legacy", replace_legacy_at_boot: bool = False,
    ):
        """
        注册Tool对象

        Args:
            tool: Tool实例
        """
        with self._registration_lock:
            self._validate_owner(owner)
            previous = self._tool_owners.get(tool.name)
            if previous is not None and previous != owner:
                # The trusted built-in bootstrap historically rebinds injected
                # host tools to attach current dependencies and permission gates.
                # This single explicit handoff is unavailable after discovery.
                boot_handoff = (
                    replace_legacy_at_boot and owner == "builtin"
                    and previous == "legacy" and self._discovery_state is None
                )
                if not boot_handoff:
                    raise PermissionError(
                        f"Tool '{tool.name}' belongs to '{previous}', not '{owner}'"
                    )
            if self._sandbox_allowed_tools is not None and tool.name in self._sandbox_allowed_tools:
                raise PermissionError("Cannot replace an approved sandbox tool")
            # One public name must resolve to exactly one execution adapter.
            if tool.name in self._functions:
                raise ValueError(f"Tool name '{tool.name}' is already registered as a function")
            if tool.name in self._tools:
                if tool.name == "ToolSearch" and self._discovery_state is not None:
                    raise PermissionError("Cannot replace the active ToolSearch control tool")
                logger.warning("工具 '%s' 已存在，将被覆盖。", tool.name)
                if self._discovery_state is not None:
                    self._discovery_state.invalidate(tool.name)

            self._tools[tool.name] = tool
            self._tool_owners[tool.name] = owner
            logger.info("工具 '%s' 已注册。", tool.name)

    def register_function(
        self, name: str, description: str, func: Callable[[str], str], *,
        owner: str = "legacy",
    ):
        """
        直接注册函数作为工具（简便方式）

        Args:
            name: 工具名称
            description: 工具描述
            func: 工具函数，接受字符串参数，返回字符串结果
        """
        with self._registration_lock:
            self._validate_owner(owner)
            previous = self._tool_owners.get(name)
            if previous is not None and previous != owner:
                raise PermissionError(
                    f"Tool '{name}' belongs to '{previous}', not '{owner}'"
                )
            if self._sandbox_allowed_tools is not None and name in self._sandbox_allowed_tools:
                raise PermissionError("Cannot override an approved sandbox tool")
            if name in self._tools:
                raise ValueError(f"Tool name '{name}' is already registered as an object")
            if name in self._functions:
                logger.warning("工具 '%s' 已存在，将被覆盖。", name)
                if self._discovery_state is not None:
                    self._discovery_state.invalidate(name)

            self._functions[name] = {
                "description": description,
                "func": func
            }
            self._tool_owners[name] = owner
            logger.info("工具 '%s' 已注册。", name)

    def unregister(self, name: str, *, owner: str = "legacy"):
        """注销工具"""
        with self._registration_lock:
            self._validate_owner(owner)
            actual = self._tool_owners.get(name)
            if actual is not None and actual != owner:
                raise PermissionError(
                    f"Tool '{name}' belongs to '{actual}', not '{owner}'"
                )
            if name == "ToolSearch" and self._discovery_state is not None:
                raise PermissionError("Cannot remove ToolSearch while discovery is enabled")
            if self._discovery_state is not None:
                self._discovery_state.invalidate(name)
            if self._sandbox_allowed_tools is not None and name in self._sandbox_allowed_tools:
                raise PermissionError("Cannot remove the approved sandbox tool")
            if name in self._tools:
                del self._tool_owners[name]
                del self._tools[name]
                logger.info("工具 '%s' 已注销。", name)
            elif name in self._functions:
                del self._tool_owners[name]
                del self._functions[name]
                logger.info("工具 '%s' 已注销。", name)
            else:
                logger.warning("工具 '%s' 不存在。", name)

    @staticmethod
    def _validate_owner(owner: str) -> None:
        if not isinstance(owner, str) or not 1 <= len(owner) <= 128 or not owner.strip():
            raise ValueError("Tool registration owner must be a non-empty name (max 128 chars)")

    def owner_of(self, name: str) -> str | None:
        """Read the current registration provenance (not a permission grant)."""
        with self._registration_lock:
            return self._tool_owners.get(name)

    def registered_owners(self, *, prefix: str = "") -> tuple[str, ...]:
        with self._registration_lock:
            return tuple(sorted({
                owner for owner in self._tool_owners.values()
                if owner.startswith(prefix)
            }))

    def replace_owned_tools(self, owner: str, tools: list[Tool]) -> None:
        """Atomically swap one source's entire inventory after discovery succeeds.

        Reconnection never leaves a stale suffix or partially published MCP
        tool set. A source cannot replace another source's published name.
        The model-step seal still rejects old adapter identities.
        """
        self._validate_owner(owner)
        new_tools = tuple(tools)
        names = [tool.name for tool in new_tools]
        if len(set(names)) != len(names) or any(
            not isinstance(name, str) or not name for name in names
        ):
            raise ValueError("Owned inventory contains invalid or duplicate tool names")
        with self._registration_lock:
            if self._sandbox_allowed_tools is not None:
                raise PermissionError("Cannot replace tools under sandbox policy")
            for name in names:
                previous = self._tool_owners.get(name)
                if previous is not None and previous != owner:
                    raise PermissionError(
                        f"Tool '{name}' belongs to '{previous}', not '{owner}'"
                    )
                if name == "ToolSearch" and self._discovery_state is not None:
                    raise PermissionError("Cannot replace active ToolSearch")
            previous_names = [
                name for name, registered_owner in self._tool_owners.items()
                if registered_owner == owner
            ]
            for name in previous_names:
                if name == "ToolSearch" and self._discovery_state is not None:
                    raise PermissionError("Cannot remove active ToolSearch")
                if self._discovery_state is not None:
                    self._discovery_state.invalidate(name)
            for name in previous_names:
                self._tools.pop(name, None)
                self._functions.pop(name, None)
                self._tool_owners.pop(name, None)
            for tool in new_tools:
                self._tools[tool.name] = tool
                self._tool_owners[tool.name] = owner

    def unregister_owner(self, owner: str) -> None:
        """Dispose only the selected source's registrations, never other owners."""
        self.replace_owned_tools(owner, [])

    def capture_tool_step(
        self, *, allowed: Callable[[str], bool] | None = None,
    ) -> ToolStepSnapshot:
        """Atomically seal the exact model-facing schemas and adapter identities.

        A model step must execute only these names with these definitions.
        Dynamic sandbox/mode/circuit checks are still evaluated at dispatch.
        """
        with self._registration_lock:
            schemas_json: list[str] = []
            bindings: dict[str, ToolStepBinding] = {}
            for spec in self.get_openai_tools():
                function = spec.get("function", {})
                name = function.get("name")
                if not isinstance(name, str) or not name:
                    continue
                if allowed is not None:
                    try:
                        if not bool(allowed(name)):
                            continue
                    except Exception:
                        # Failure to retrieve active mode/authorization fails closed.
                        continue
                adapter = self._tools.get(name)
                if adapter is None:
                    adapter = self._functions.get(name)
                if adapter is None:
                    continue
                try:
                    encoded = encode_tool_schema(spec)
                except (TypeError, ValueError):
                    logger.warning("Skipping tool %s with non-JSON schema", name)
                    continue
                schemas_json.append(encoded)
                bindings[name] = ToolStepBinding(adapter, schema_fingerprint(spec))
            return ToolStepSnapshot.create(self, schemas_json, bindings)

    def get_openai_tools(self, *, include_deferred: bool = False) -> list[dict[str, Any]]:
        """
        构建 OpenAI function calling 所需的 tools 列表。

        Returns:
            list of {"type": "function", "function": {name, description, parameters}}
        """
        with self._registration_lock:
            tools: list[dict[str, Any]] = []
    
            for tool in tuple(self._tools.values()):
                if not self._tool_allowed(tool.name) or not self._circuit_breaker.is_available(tool.name):
                    continue
                if not self._discovery_mode_allowed(tool.name):
                    continue
                if not include_deferred and not self._discovery_visible(tool.name):
                    continue
                try:
                    tools.append(self._tool_schema_spec(tool))
                except Exception:
                    # Broken declarations must never become empty schemas.
                    logger.warning("Skipping tool %s with unavailable schema", tool.name)
                    continue
    
            for name, info in tuple(self._functions.items()):
                if not self._tool_allowed(name) or not self._circuit_breaker.is_available(name):
                    continue
                if not self._discovery_mode_allowed(name):
                    continue
                if not include_deferred and not self._discovery_visible(name):
                    continue
                tools.append({
                    "type": "function",
                    "function": {
                        "name": name,
                        "description": info.get("description", "") if isinstance(info, dict) else "",
                        "parameters": {
                            "type": "object",
                            "properties": {
                                "input": {
                                    "type": "string",
                                    "description": "raw input string",
                                }
                            },
                            "required": ["input"],
                            "additionalProperties": False,
                        },
                    },
                })
    
            return tools

    @classmethod
    def _tool_schema_spec(cls, tool: Tool) -> dict[str, Any]:
        schema_provider = getattr(tool, "get_input_schema", None)
        if callable(schema_provider):
            # Full JSON Schema is authoritative and may not be representable
            # by legacy ToolParameter (nullable types, empty property names).
            params_schema = schema_provider()
            compile_mcp_input_schema(params_schema)
        else:
            params_schema = cls._parameters_to_schema(tool.get_parameters())
        if not isinstance(params_schema, dict) or params_schema.get("type") != "object":
            raise ValueError("Tool input schema must be an object schema")
        properties = params_schema.get("properties", {})
        required = params_schema.get("required", [])
        if (not isinstance(properties, dict)
                or not all(isinstance(name, str) and isinstance(spec, (dict, bool))
                           for name, spec in properties.items())
                or not isinstance(required, list)
                or not all(isinstance(name, str) for name in required)
                or len(set(required)) != len(required)):
            raise ValueError("Tool input schema has invalid properties or required fields")
        spec = {
            "type": "function",
            "function": {
                "name": tool.name,
                "description": tool.description or "",
                "parameters": params_schema,
            },
        }
        encode_tool_schema(spec)
        return spec

    @staticmethod
    def _parameters_to_schema(params: list[ToolParameter]) -> dict[str, Any]:
        """
        将 ToolParameter 列表转换为 JSON Schema。
        """
        if not isinstance(params, (list, tuple)):
            raise TypeError("Tool parameters must be a list or tuple")
        properties: dict[str, Any] = {}
        required: list[str] = []
        for p in params:
            if not isinstance(p, ToolParameter) or not p.name or p.name in properties:
                raise ValueError("Tool parameter declaration is invalid or duplicated")
            param_type = (p.type or "string").strip().lower()
            schema: dict[str, Any] = {
                "type": ToolRegistry._normalize_schema_type(param_type),
                "description": p.description or "",
            }
            if schema["type"] == "array":
                # OpenAI-compatible function schemas expect an `items` schema for arrays.
                schema.setdefault("items", {"type": "string"})
            if p.default is not None:
                schema["default"] = p.default
            properties[p.name] = schema
            if p.required:
                required.append(p.name)

        return {
            "type": "object",
            "properties": properties,
            "required": required,
            "additionalProperties": False,
        }

    @staticmethod
    def _normalize_schema_type(param_type: str) -> str:
        """
        规范化参数类型到 JSON Schema 类型。
        """
        mapping = {
            "str": "string",
            "string": "string",
            "int": "integer",
            "integer": "integer",
            "float": "number",
            "number": "number",
            "bool": "boolean",
            "boolean": "boolean",
            "array": "array",
            "list": "array",
            "object": "object",
            "dict": "object",
        }
        return mapping.get(param_type, "string")

    def get_tool(self, name: str) -> Optional[Tool]:
        """获取Tool对象"""
        if not self._tool_allowed(name):
            return None
        return self._tools.get(name)

    def get_function(self, name: str) -> Optional[Callable]:
        """获取工具函数"""
        if not self._tool_allowed(name):
            return None
        func_info = self._functions.get(name)
        return func_info["func"] if func_info else None

    def execute_tool(
        self, name: str, input_text, *, snapshot: ToolStepSnapshot | None = None,
    ) -> str:
        """
        执行工具

        Args:
            name: 工具名称
            input_text: 输入参数

        Returns:
            工具执行结果（符合《通用工具响应协议》的 JSON 字符串）
        """
        result_payload: Optional[dict] = None
        
        # 准备参数
        if isinstance(input_text, dict):
            parameters = input_text.copy()  # 复制以避免修改原始参数
        else:
            parameters = {"input": input_text}
        
        # Hosted membership is re-read for every invocation, including direct
        # registry calls bypassing the model and calls with stale snapshots.
        if not self._hosted_tool_allowed(name):
            message = "Hosted tool access denied or project membership unavailable."
            return json.dumps({
                "status": ToolStatus.ERROR.value, "data": {}, "text": message,
                "error": {"code": ErrorCode.PERMISSION_DENIED.value, "message": message},
                "stats": {"time_ms": 0},
                "context": {"cwd": ".", "params_input": parameters},
            }, ensure_ascii=False)

        # Deny before optimistic-lock injection or any adapter execution.
        if not self._tool_allowed(name):
            return json.dumps({
                "status": ToolStatus.ERROR.value,
                "data": {},
                "text": "Tool blocked by Docker sandbox policy.",
                "error": {
                    "code": ErrorCode.ACCESS_DENIED.value,
                    "message": "Tool blocked by Docker sandbox policy.",
                },
                "stats": {"time_ms": 0},
                "context": {"cwd": ".", "params_input": parameters},
            }, ensure_ascii=False)

        # The same dynamic mode policy used for catalog filtering must also
        # guard direct registry dispatch, even without Agent pre-hooks.
        if not self._discovery_mode_allowed(name):
            message = f"Tool '{name}' is not permitted in the current Agent mode."
            return json.dumps({
                "status": ToolStatus.ERROR.value, "data": {}, "text": message,
                "error": {"code": ErrorCode.PERMISSION_DENIED.value, "message": message},
                "stats": {"time_ms": 0},
                "context": {"cwd": ".", "params_input": parameters},
            }, ensure_ascii=False)

        # Deferred discovery is enforced independently of the LLM tool list.
        # Unadvertised tool calls from stale contexts/plugins cannot execute.
        if not self._discovery_visible(name):
            message = f"Tool '{name}' requires discovery via ToolSearch before execution."
            return json.dumps({
                "status": ToolStatus.ERROR.value, "data": {}, "text": message,
                "error": {"code": "DISCOVERY_REQUIRED", "message": message},
                "stats": {"time_ms": 0},
                "context": {"cwd": ".", "params_input": parameters},
            }, ensure_ascii=False)

        # Bind the executor *under the same lock* as registration mutations.
        # Release before adapter I/O: safe reads must remain parallelizable.
        with self._registration_lock:
            tool = self._tools.get(name)
            function_info = self._functions.get(name)
            if snapshot is not None:
                binding = (
                    snapshot.bindings.get(name)
                    if isinstance(snapshot, ToolStepSnapshot) and snapshot.registry is self
                    else None
                )
                adapter = tool if tool is not None else function_info
                valid = binding is not None and adapter is binding.adapter
                if valid:
                    # Also deny in-place schema changes and changed permissions
                    # (including revocation during the model's response time).
                    current = next(
                        (spec for spec in self.get_openai_tools()
                         if spec["function"]["name"] == name), None,
                    )
                    try:
                        valid = (
                            current is not None
                            and schema_fingerprint(current) == binding.schema_hash
                        )
                    except (TypeError, ValueError):
                        valid = False
                if not valid:
                    message = f"Tool '{name}' changed or was not advertised in this model step."
                    return json.dumps({
                        "status": ToolStatus.ERROR.value, "data": {}, "text": message,
                        "error": {"code": "STALE_TOOL_SCHEMA", "message": message},
                        "stats": {"time_ms": 0},
                        "context": {"cwd": ".", "params_input": parameters},
                    }, ensure_ascii=False)

        # =====================================================================
        # 乐观锁自动注入：为 Write/Edit 注入 expected_mtime_ms / expected_size_bytes
        # =====================================================================
        if name in {"Write", "Edit", "MultiEdit"}:
            parameters = self._inject_optimistic_lock_params(name, parameters)
        
        # 熔断检查
        if not self._circuit_breaker.is_available(name):
            payload = {
                "status": ToolStatus.ERROR.value,
                "data": {},
                "text": f"工具 '{name}' 因连续失败被临时禁用。",
                "error": {
                    "code": ErrorCode.CIRCUIT_OPEN.value,
                    "message": f"工具 '{name}' 因连续失败被临时禁用。",
                },
                "stats": {"time_ms": 0},
                "context": {"cwd": ".", "params_input": parameters},
            }
            return json.dumps(payload, ensure_ascii=False, indent=2)

        # A malformed legacy declaration must not execute just because the
        # adapter did not opt in to strict argument validation.
        if tool is not None:
            try:
                self._tool_schema_spec(tool)
            except Exception:
                message = "Tool parameter schema is unavailable."
                error_code = getattr(type(tool), "schema_error_code", ErrorCode.INVALID_PARAM)
                if not isinstance(error_code, ErrorCode):
                    error_code = ErrorCode.INVALID_PARAM
                return json.dumps({
                    "status": ToolStatus.ERROR.value, "data": {}, "text": message,
                    "error": {"code": error_code.value, "message": message},
                    "stats": {"time_ms": 0},
                    "context": {"cwd": ".", "params_input": parameters},
                }, ensure_ascii=False)

        # Validate the *final* parameters immediately before dispatch.
        # Hooks have already rewritten inputs in ToolRuntime; optimistic-lock
        # metadata has already been injected. Neither may bypass this gate.
        # Sandbox allowlist and open circuits are checked above, first.
        if tool is not None and getattr(tool, "strict_input_contract", False):
            try:
                validation_error = validate_tool_input(parameters, tool.get_parameters())
            except Exception:
                logger.exception("Invalid schema for tool %s", name)
                validation_error = "Tool parameter schema is unavailable."
            if validation_error:
                return json.dumps({
                    "status": ToolStatus.ERROR.value,
                    "data": {},
                    "text": validation_error,
                    "error": {"code": ErrorCode.INVALID_PARAM.value, "message": validation_error},
                    "stats": {"time_ms": 0},
                    "context": {"cwd": ".", "params_input": parameters},
                }, ensure_ascii=False)

        # Legacy function tools publish a single string-valued 'input' field.
        # Validate that exact contract and unwrap it before calling the
        # registered Callable[[str], str]; never forward an arbitrary dict.
        if function_info is not None and tool is None:
            if set(parameters) != {"input"} or not isinstance(parameters["input"], str):
                return json.dumps({
                    "status": ToolStatus.ERROR.value,
                    "data": {},
                    "text": "Function tool requires a single string 'input' parameter.",
                    "error": {
                        "code": ErrorCode.INVALID_PARAM.value,
                        "message": "Function tool requires a single string 'input' parameter.",
                    },
                    "stats": {"time_ms": 0},
                    "context": {"cwd": ".", "params_input": parameters},
                }, ensure_ascii=False)

        # 优先查找Tool对象
        if tool is not None:
            try:
                result = tool.run(parameters)
                result_payload = self._normalize_result(name, result, parameters)
            except Exception as e:
                result_payload = self._create_internal_error_payload(
                    name=name,
                    message=f"执行工具 '{name}' 时发生异常: {str(e)}",
                    params_input=parameters,
                )

        # 查找函数工具
        elif function_info is not None:
            func = function_info["func"]
            try:
                result = func(parameters["input"])
                result_payload = self._normalize_result(name, result, parameters)
            except Exception as e:
                result_payload = self._create_internal_error_payload(
                    name=name,
                    message=f"执行工具 '{name}' 时发生异常: {str(e)}",
                    params_input=parameters,
                )

        else:
            result_payload = self._create_internal_error_payload(
                name=name,
                message=f"未找到名为 '{name}' 的工具。",
                params_input={},
            )
        
        # 熔断记录 — 只计执行错误，不计参数错误
        # INVALID_PARAM / NOT_FOUND / CONFLICT 是模型调用方式问题，
        # 不是工具本身故障，不应触发熔断
        _CIRCUIT_SAFE_ERRORS = {
            ErrorCode.INVALID_PARAM, ErrorCode.MCP_PARAM_ERROR,
            ErrorCode.INTERRUPTED,
            ErrorCode.ACCESS_DENIED, ErrorCode.NOT_FOUND, ErrorCode.CONFLICT,
            ErrorCode.IS_DIRECTORY, ErrorCode.BINARY_FILE,
            ErrorCode.ASK_USER_UNAVAILABLE,
        }
        if isinstance(result_payload, dict):
            status = result_payload.get("status")
            if status == ToolStatus.ERROR.value:
                err = result_payload.get("error", {}) or {}
                err_code_raw = err.get("code") if isinstance(err, dict) else None
                err_msg = err.get("message") if isinstance(err, dict) else None
                # 白名单错误码不计入熔断
                try:
                    err_code = ErrorCode(err_code_raw) if err_code_raw else None
                except ValueError:
                    err_code = None
                if err_code in _CIRCUIT_SAFE_ERRORS:
                    self._circuit_breaker.record_success(name)
                else:
                    self._circuit_breaker.record_failure(
                        name, str(err_msg or result_payload.get("text") or ""))
            else:
                self._circuit_breaker.record_success(name)
        
        # =====================================================================
        # 乐观锁缓存更新：记录 Read 结果的元信息
        # =====================================================================
        if name == "Read":
            self._cache_read_meta(result_payload, parameters)

        return json.dumps(result_payload, ensure_ascii=False, indent=2)

    def export_read_cache(self) -> dict[str, ReadMeta]:
        """导出 Read 缓存（用于会话持久）。"""
        return dict(self._read_cache)

    def import_read_cache(self, data: dict[str, ReadMeta]) -> None:
        """恢复 Read 缓存（用于会话持久）。"""
        if isinstance(data, dict):
            self._read_cache = dict(data)
    
    def _inject_optimistic_lock_params(self, tool_name: str, parameters: dict) -> dict:
        """
        为 Write/Edit 工具自动注入乐观锁参数
        
        如果参数中缺少 expected_mtime_ms / expected_size_bytes，
        尝试从 Read 缓存中查找并注入。
        
        Args:
            tool_name: 工具名称
            parameters: 原始参数
            
        Returns:
            注入后的参数（可能与原始相同）
        """
        # 如果已经提供了，不覆盖
        if "expected_mtime_ms" in parameters and "expected_size_bytes" in parameters:
            return parameters
        
        # 获取目标路径
        path = parameters.get("path")
        if not path:
            return parameters
        
        # 尝试从缓存查找（先用原始 path，再用规范化 path）
        meta = self._read_cache.get(path)
        if not meta:
            # 尝试规范化路径匹配
            # 注意：这里的规范化逻辑应与工具内部一致
            normalized_path = path.replace("\\", "/")
            if normalized_path.startswith("./"):
                normalized_path = normalized_path[2:]
            meta = self._read_cache.get(normalized_path)
        
        if meta:
            # 找到缓存，注入参数
            if "expected_mtime_ms" not in parameters:
                parameters["expected_mtime_ms"] = meta["file_mtime_ms"]
            if "expected_size_bytes" not in parameters:
                parameters["expected_size_bytes"] = meta["file_size_bytes"]
            logger.debug(
                f"[OptimisticLock] Auto-injected for {tool_name}: "
                f"mtime={meta['file_mtime_ms']}, size={meta['file_size_bytes']}, path={path}"
            )
        else:
            # 未找到缓存，让工具正常报错（提示先 Read）
            logger.debug(
                f"[OptimisticLock] No Read cache found for path '{path}'. "
                f"Tool will report INVALID_PARAM if file exists."
            )
        
        return parameters
    
    def _cache_read_meta(self, result: Any, params_input: dict) -> None:
        """
        缓存 Read 工具的元信息（用于后续 Write/Edit 的乐观锁校验）
        
        仅在 Read 成功或 partial 时缓存。
        
        Args:
            result_str: Read 工具的响应字符串
            params_input: 原始输入参数
        """
        if isinstance(result, dict):
            parsed = result
        else:
            try:
                parsed = json.loads(result)
            except json.JSONDecodeError:
                return
        
        # 仅缓存成功/partial 状态
        status = parsed.get("status")
        if status not in ("success", "partial"):
            return
        
        # 提取元信息
        stats = parsed.get("stats", {})
        context = parsed.get("context", {})
        
        file_mtime_ms = stats.get("file_mtime_ms")
        file_size_bytes = stats.get("file_size_bytes")
        path_resolved = context.get("path_resolved")
        
        # 必须同时有 mtime 和 size
        if file_mtime_ms is None or file_size_bytes is None:
            logger.warning(
                f"[OptimisticLock] Read response missing file_mtime_ms or file_size_bytes. "
                f"Skipping cache."
            )
            return
        
        # 构建缓存条目
        meta: ReadMeta = {
            "path_resolved": path_resolved or "",
            "file_mtime_ms": file_mtime_ms,
            "file_size_bytes": file_size_bytes,
            "captured_at": time.time(),
        }
        
        # 使用 path_resolved 作为主键
        if path_resolved:
            self._read_cache[path_resolved] = meta
        
        # 同时用原始 path 作为别名键（便于匹配）
        original_path = params_input.get("path")
        if original_path and original_path != path_resolved:
            self._read_cache[original_path] = meta
        
        logger.debug(
            f"[OptimisticLock] Cached Read meta: path={path_resolved}, "
            f"mtime={file_mtime_ms}, size={file_size_bytes}"
        )
    
    def clear_read_cache(self) -> None:
        """
        清空 Read 元信息缓存
        
        在需要重置乐观锁状态时调用（如新会话开始）。
        """
        self._read_cache.clear()
        logger.debug("[OptimisticLock] Read cache cleared.")
    
    def _normalize_result(self, tool_name: str, result: Any, params_input: Any) -> dict:
        if isinstance(result, dict):
            payload = result
        elif isinstance(result, str):
            try:
                payload = json.loads(result)
            except json.JSONDecodeError:
                return self._create_internal_error_payload(
                    name=tool_name,
                    message=f"Tool '{tool_name}' returned invalid JSON.",
                    params_input=params_input if isinstance(params_input, dict) else {"input": params_input},
                )
        else:
            return self._create_internal_error_payload(
                name=tool_name,
                message=f"Tool '{tool_name}' returned unsupported result type.",
                params_input=params_input if isinstance(params_input, dict) else {"input": params_input},
            )

        if not isinstance(payload, dict):
            return self._create_internal_error_payload(
                name=tool_name,
                message=f"Tool '{tool_name}' returned invalid payload.",
                params_input=params_input if isinstance(params_input, dict) else {"input": params_input},
            )

        status = payload.get("status")
        if status not in (ToolStatus.SUCCESS.value, ToolStatus.PARTIAL.value, ToolStatus.ERROR.value):
            return self._create_internal_error_payload(
                name=tool_name,
                message=f"Tool '{tool_name}' returned non-protocol response.",
                params_input=params_input if isinstance(params_input, dict) else {"input": params_input},
            )

        data = payload.get("data")
        if not isinstance(data, dict):
            data = {}

        text = payload.get("text") or ""
        if not isinstance(text, str):
            text = str(text)

        stats = payload.get("stats")
        if not isinstance(stats, dict):
            stats = {}
        stats.setdefault("time_ms", 0)

        context = payload.get("context")
        if not isinstance(context, dict):
            context = {}
        context.setdefault("cwd", ".")
        if "params_input" not in context:
            context["params_input"] = (
                params_input if isinstance(params_input, dict) else {"input": params_input}
            )

        normalized = {
            "status": status,
            "data": data,
            "text": text,
            "stats": stats,
            "context": context,
        }

        if status == ToolStatus.ERROR.value:
            error = payload.get("error")
            if not isinstance(error, dict):
                error = {
                    "code": ErrorCode.INTERNAL_ERROR.value,
                    "message": text or "Tool execution error",
                }
            else:
                error.setdefault("code", ErrorCode.INTERNAL_ERROR.value)
                error.setdefault("message", text or "Tool execution error")
            normalized["error"] = error

        return normalized

    def _create_internal_error_payload(self, name: str, message: str, params_input: dict) -> dict:
        """创建内部错误响应 payload（符合协议）"""
        return {
            "status": ToolStatus.ERROR.value,
            "data": {},
            "text": message,
            "error": {
                "code": ErrorCode.INTERNAL_ERROR.value,
                "message": message,
            },
            "stats": {"time_ms": 0},
            "context": {
                "cwd": ".",
                "params_input": params_input,
            },
        }

    def _create_internal_error_response(self, name: str, message: str, params_input: dict) -> str:
        """创建内部错误响应（符合协议）"""
        payload = self._create_internal_error_payload(name, message, params_input)
        return json.dumps(payload, ensure_ascii=False, indent=2)

    def get_tools_description(self) -> str:
        """
        获取所有可用工具的格式化描述字符串

        Returns:
            工具描述字符串，用于构建提示词
        """
        descriptions = []

        # Tool对象描述
        for tool in tuple(self._tools.values()):
            if (self._tool_allowed(tool.name)
                    and self._discovery_mode_allowed(tool.name)
                    and (self._discovery_state is None
                         or self._discovery_state.is_visible(tool.name))):
                descriptions.append(f"- {tool.name}: {tool.description}")

        # 函数工具描述
        for name, info in tuple(self._functions.items()):
            if (self._tool_allowed(name)
                    and self._discovery_mode_allowed(name)
                    and (self._discovery_state is None
                         or self._discovery_state.is_visible(name))):
                descriptions.append(f"- {name}: {info['description']}")

        return "\n".join(descriptions) if descriptions else "暂无可用工具"

    def list_tools(self) -> list[str]:
        """列出所有工具名称"""
        return [name for name in [*tuple(self._tools), *tuple(self._functions)] if self._tool_allowed(name)]

    def get_disabled_tools(self) -> list[str]:
        """获取当前被熔断禁用的工具列表。"""
        return self._circuit_breaker.get_disabled_tools()

    def get_all_tools(self) -> list[Tool]:
        """获取所有Tool对象（internal view, including deferred definitions）."""
        return [tool for tool in tuple(self._tools.values()) if self._tool_allowed(tool.name)]

    def get_prompt_tools(self) -> list[Tool]:
        """Only tools currently visible to the model may inject usage notes."""
        return [
            tool for tool in self.get_all_tools()
            if self._discovery_mode_allowed(tool.name)
            and (self._discovery_state is None or self._discovery_state.is_visible(tool.name))
            and self._circuit_breaker.is_available(tool.name)
        ]

    def clear(self):
        """清空所有工具"""
        with self._registration_lock:
            if self._sandbox_allowed_tools is not None:
                raise PermissionError("Cannot clear registry while sandbox policy is active")
            if self._discovery_state is not None:
                for name in self.list_tools():
                    self._discovery_state.invalidate(name)
            self._tools.clear()
            self._functions.clear()
            self._tool_owners.clear()
            logger.info("所有工具已清空。")

# 全局工具注册表
global_registry = ToolRegistry()
