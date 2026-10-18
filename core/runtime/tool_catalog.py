"""Session-local, opt-in deferred discovery over the canonical ToolRegistry.

The registry remains the only authority for registration, schema and execution.
This module stores names promoted by the model, not a second set of executors.
Catalog visibility is NEVER a replacement for plan/delegate/host authorization.
"""
from __future__ import annotations

import re
import threading
from collections.abc import Callable
from typing import Any

from tools.base import ErrorCode, Tool, ToolParameter

# Keep file/code primitives available; defer optional MCP, team and plugin
# integrations until searched. Callers may override this explicit allowlist.
DEFAULT_ALWAYS_VISIBLE: frozenset[str] = frozenset({
    "Read", "Write", "Edit", "MultiEdit", "LS", "Glob", "Grep",
    "Bash", "TodoWrite", "AskUser", "Task", "TaskOutput", "Skill",
    "EnterPlanMode", "ExitPlanMode", "ToolSearch",
})


class ToolDiscoveryState:
    """A bounded, revocable promotion set scoped to exactly one registry."""

    def __init__(
        self,
        always_visible: frozenset[str] = DEFAULT_ALWAYS_VISIBLE,
        *,
        max_promoted: int = 18,
    ):
        self.always_visible = frozenset(always_visible) | {"ToolSearch"}
        self.max_promoted = max(1, min(int(max_promoted), 48))
        self._promoted: dict[str, None] = {}
        # Search returns a proposal; visibility changes only at the next
        # explicit model-schema binding, never during the tool batch itself.
        self._pending: dict[str, None] = {}
        self._lock = threading.RLock()

    def is_visible(self, name: str) -> bool:
        if name in self.always_visible:
            return True
        with self._lock:
            return name in self._promoted

    def invalidate(self, name: str) -> None:
        """Replacing/removing a tool revokes approval for its old executable."""
        with self._lock:
            self._promoted.pop(name, None)
            self._pending.pop(name, None)

    def pending_names(self) -> frozenset[str]:
        with self._lock:
            return frozenset(self._pending)

    def activate_pending(self, allowed: Callable[[str], bool]) -> None:
        """Commit discoveries at the model schema boundary, not mid-batch."""
        with self._lock:
            for name in tuple(self._pending):
                try:
                    permitted = bool(allowed(name))
                except Exception:
                    permitted = False
                if permitted and len(self._promoted) < self.max_promoted:
                    self._promoted[name] = None
            self._pending.clear()

    def promoted_names(self) -> frozenset[str]:
        with self._lock:
            return frozenset(self._promoted)

    def search(
        self,
        catalog: list[dict[str, Any]],
        query: str,
        *,
        limit: int,
        can_promote: Callable[[str], bool],
    ) -> list[dict[str, str]]:
        """Rank schema-backed candidates; promote only authorized matches.

        Candidates come from the exact registry schema builder. No unregistered
        tool or invalid schema can enter the discovered set. No user input is
        interpolated into an execution command.
        """
        words = re.findall(r"[\w-]+", query.casefold())[:8]
        if not words:
            return []
        def permitted(name: str) -> bool:
            try:
                return bool(can_promote(name))
            except Exception:
                # Never reveal a tool when an authorization callback fails.
                return False

        ranked: list[tuple[int, str, str]] = []
        for tool in catalog:
            fn = tool.get("function", {})
            name = fn.get("name", "")
            description = fn.get("description", "")
            if not isinstance(name, str) or not isinstance(description, str):
                continue
            if self.is_visible(name) or not permitted(name):
                continue
            # Bound work over third-party tool descriptions. The final model
            # schema may be larger, but search never indexes unbounded text.
            haystack = (name + " " + description[:1024]).casefold()
            name_lower = name.casefold()
            # Strong name matches beat broad description matches; prevent
            # unrelated tools from becoming promoted on a single token.
            matched = sum(word in haystack for word in words)
            if matched == 0:
                continue
            score = (100 if name_lower == query.casefold().strip() else 0) + (
                30 if all(word in name_lower for word in words) else 0
            ) + matched * 4 + (8 if all(word in haystack for word in words) else 0)
            ranked.append((score, name, description))
        ranked.sort(key=lambda item: (-item[0], item[1]))
        matches: list[dict[str, str]] = []
        with self._lock:
            for _, name, description in ranked:
                if (len(matches) >= limit
                        or len(self._promoted) + len(self._pending) >= self.max_promoted):
                    break
                if name in self._promoted or name in self._pending or not permitted(name):
                    continue
                self._pending[name] = None
                matches.append({"name": name, "description": description[:240]})
        return matches


class ToolSearchTool(Tool):
    worker_parent_only = True
    """Model-callable discovery with registry-scoped promotion and policy."""

    strict_input_contract = True

    def __init__(
        self,
        registry: Any,
        state: ToolDiscoveryState,
        *,
        can_promote: Callable[[str], bool],
    ):
        super().__init__(
            name="ToolSearch",
            description=(
                "Find and enable additional tools by name or capability. "
                "Use before invoking MCP, team or optional integrations. "
                "Results are available as tools on the next model step."
            ),
        )
        self._registry = registry
        self._state = state
        self._can_promote = can_promote

    def get_parameters(self) -> list[ToolParameter]:
        return [
            ToolParameter(
                name="query", type="string", required=True,
                description="A specific tool name or capability to look up.",
            ),
            ToolParameter(
                name="limit", type="integer", required=False, default=5,
                description="Maximum tools to return (1 to 6).",
            ),
        ]

    def run(self, parameters: dict[str, Any]) -> str:
        query = parameters.get("query")
        limit = parameters.get("limit", 5)
        if (
            not isinstance(query, str) or len(query.strip()) < 2
            or len(query) > 128 or type(limit) is not int
            or not 1 <= limit <= 6
        ):
            return self.create_error_response(
                ErrorCode.INVALID_PARAM,
                "Provide a specific 2-128 character query and limit from 1 to 6.",
                parameters,
            )
        # Use the same canonical schema path as the model tool binding.
        # The registry applies sandbox and circuit filters before listing.
        catalog = self._registry.get_openai_tools(include_deferred=True)
        matches = self._state.search(
            catalog, query.strip(), limit=limit, can_promote=self._can_promote,
        )
        text = (
            "Discovered tools: " + ", ".join(match["name"] for match in matches)
            + ". Call the tool on the next step."
            if matches else "No permitted deferred tools match; refine the query."
        )
        return self.create_success_response(
            data={"matches": matches, "count": len(matches)},
            text=text,
            params_input=parameters,
            time_ms=1,
        )
