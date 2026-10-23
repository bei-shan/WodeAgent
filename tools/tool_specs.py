"""Declarative tool manifest.

Every tool gets capability metadata in one place instead of three: the registry
uses it for inventory and concurrency, the Web UI for grouping, and the prompt
builder for deciding what to advertise. Tools may override the table with a
class-level ``spec`` attribute, but most do not need to.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

KINDS = ("read", "write", "exec", "network", "agent", "control")
APPROVALS = ("auto", "ask", "deny")
EXPOSURES = ("core", "deferred")

# Tools advertised in every turn. Everything else stays callable but is only
# named in a one-line footer, keeping the system prompt small.
CORE_TOOLS = frozenset({
    "Read", "Write", "Edit", "Bash", "Glob", "Grep", "Task", "TodoWrite",
    "WebFetch", "WebSearch", "AskUser", "Skill", "Goal", "Memory",
})


@dataclass(frozen=True)
class ToolSpec:
    kind: str = "control"
    read_only: bool = False
    approval: str = "auto"
    exposure: str = "core"
    output_budget: int = 16_000
    group: str = "general"

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "read_only": self.read_only,
            "approval": self.approval,
            "exposure": self.exposure,
            "output_budget": self.output_budget,
            "group": self.group,
        }


SPECS: dict[str, ToolSpec] = {
    # Files
    "Read": ToolSpec("read", True, group="files", output_budget=60_000),
    "Glob": ToolSpec("read", True, group="files", output_budget=16_000),
    "Grep": ToolSpec("read", True, group="files", output_budget=24_000),
    "LS": ToolSpec("read", True, exposure="deferred", group="files"),
    "Write": ToolSpec("write", approval="ask", group="files"),
    "Edit": ToolSpec("write", approval="ask", group="files"),
    "MultiEdit": ToolSpec("write", approval="ask", exposure="deferred", group="files"),
    # Runtime
    "Bash": ToolSpec("exec", approval="ask", group="runtime", output_budget=24_000),
    # Agents and orchestration
    "Task": ToolSpec("agent", group="agents"),
    "TaskOutput": ToolSpec("agent", exposure="deferred", group="agents"),
    "SendMessage": ToolSpec("agent", exposure="deferred", group="agents"),
    "Workflow": ToolSpec("agent", exposure="deferred", group="agents"),
    "BrowserUse": ToolSpec("network", approval="ask", exposure="deferred", group="web"),
    # Web
    "WebFetch": ToolSpec("network", group="web"),
    "WebSearch": ToolSpec("network", group="web"),
    # Interaction and planning
    "AskUser": ToolSpec("control", group="interaction"),
    "Skill": ToolSpec("read", True, group="interaction"),
    "TodoWrite": ToolSpec("control", group="interaction"),
    "Goal": ToolSpec("control", group="planning"),
    "Memory": ToolSpec("write", group="memory"),
    # Modes and worktrees are controls, not capabilities the model must browse.
    "EnterPlanMode": ToolSpec("control", exposure="deferred", group="planning"),
    "ExitPlanMode": ToolSpec("control", exposure="deferred", group="planning"),
    "EnterWorktree": ToolSpec("exec", exposure="deferred", group="runtime"),
    "ExitWorktree": ToolSpec("exec", exposure="deferred", group="runtime"),
}

TEAM_TOOLS = (
    "TeamCreate", "TeamDelete", "TeamList", "TeamStatus", "TeamFanout",
    "TeamCollect", "TeamCleanup", "TeamRetry", "TeamTaskCreate", "TeamTaskGet",
    "TeamTaskList", "TeamTaskUpdate", "TeamApprovePlan", "TeamApprovals",
)
SPECS.update({
    name: ToolSpec("agent", exposure="deferred", group="team") for name in TEAM_TOOLS
})

DEFAULT_SPEC = ToolSpec()


def spec_for(name: str) -> ToolSpec:
    return SPECS.get(str(name or ""), DEFAULT_SPEC)


def describe_tool(tool: Any) -> dict[str, Any]:
    """Manifest entry for a Tool instance, honouring a per-tool override."""
    name = str(getattr(tool, "name", "") or "")
    spec = getattr(tool, "spec", None)
    if not isinstance(spec, ToolSpec):
        spec = spec_for(name)
    description = " ".join(str(getattr(tool, "description", "") or "").split())
    return {
        "name": name,
        "description": description[:200],
        "kind": spec.kind,
        "read_only": spec.read_only,
        "approval": spec.approval,
        "exposure": spec.exposure,
        "group": spec.group,
        "output_budget": spec.output_budget,
    }
