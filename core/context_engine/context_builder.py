"""Context builder for ReAct prompt assembly — Late Binding (Pi-inspired).

Each ``build_messages()`` call assembles the full message list from
live data sources (tool registry, MCP status, skills, CODE_LAW).
No full-system-message cache — only cheap individual caches (L1 text,
CODE_LAW mtime, L1 file mtime) that never need manual invalidation.

Messages 格式：
[
  {"role": "system", "content": "L1 系统提示 + 工具 usage_notes"},
  {"role": "user", "content": "untrusted CODE_LAW/MCP/Skills reference data"},
  {"role": "user", "content": "...问题..."},
  {"role": "assistant", "content": "...", "tool_calls": [...]},
  {"role": "tool", "tool_call_id": "...", "content": "{截断后JSON}"},
  ...
]
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional, Dict, Any

from core.context_engine.instruction_files import (
    instruction_signature, load_instructions,
)
from core.context_engine.untrusted_content import (
    TRUST_BOUNDARY_RULES, frame_history_tool_results, untrusted_reference,
)


@dataclass
class ContextBuilder:
    """构建 ReAct 循环的 messages 列表 — Late Binding 模式。

    每次 ``build_messages()`` 实时组装，无全局缓存。
    仅保留轻量缓存：L1 文件 mtime、CODE_LAW 文本 + mtime。
    工具提示词从 Tool.usage_notes 实时获取（纯内存操作）。
    """

    tool_registry: "ToolRegistry"  # noqa: F821
    project_root: str
    system_prompt_override: Optional[str] = None
    trusted_prompt_root: Optional[str] = None  # trusted framework/tests only
    mcp_tools_prompt: Optional[str] = None
    skills_prompt: Optional[str] = None

    # ── Lightweight caches (self-invalidating) ──
    _cached_l1_mtime: Optional[float] = field(default=None, init=False)
    _cached_l1_text: str = field(default="", init=False)
    _cached_instructions: str = field(default="", init=False)
    _cached_instruction_signature: tuple = field(default=(), init=False)

    # ── Live state (no cache invalidation needed) ──
    _runtime_system_blocks: List[str] = field(default_factory=list, init=False)
    _output_style_prompt: str = field(default="", init=False)
    _mcp_tools_prompt: str = field(default="", init=False)
    _skills_prompt: str = field(default="", init=False)
    _memory_prompt: str = field(default="", init=False)

    # ══════════════════════════════════════════════════════════════
    # Public API
    # ══════════════════════════════════════════════════════════════

    def build_messages(
        self,
        history_messages: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        """构建完整的 messages 列表（实时组装）。

        Args:
            history_messages: HistoryManager.to_messages() 的历史消息

        Returns:
            完整的 messages 列表，可直接传给 LLM
        """
        messages: List[Dict[str, Any]] = []
        messages.extend(self._build_system_messages())
        messages.extend(self.get_reference_messages())
        messages.extend(frame_history_tool_results(history_messages))
        return messages

    def get_system_messages(self) -> List[Dict[str, Any]]:
        """获取 system messages（供日志/快照使用）。"""
        return self._build_system_messages()

    def get_reference_messages(self) -> List[Dict[str, Any]]:
        """Project and extension data has no system-message authority."""
        refs: List[Dict[str, Any]] = []
        # Tool usage_notes can originate in plugins/MCP adapters. Never let
        # registry-controlled prose modify the trusted bundled L1 prompt.
        tool_notes = self._tool_usage_notes()
        if tool_notes:
            refs.append(untrusted_reference("tool registry usage notes", tool_notes))
        if self._output_style_prompt:
            refs.append(untrusted_reference("output style preference", self._output_style_prompt))
        if self._mcp_tools_prompt:
            refs.append(untrusted_reference("MCP tool descriptions", self._mcp_tools_prompt))
        if self._skills_prompt:
            refs.append(untrusted_reference("Skill metadata", self._skills_prompt))
        code_law = self._load_instructions()
        if code_law:
            refs.append(untrusted_reference(
                "workspace instructions (AGENTS.md / CLAUDE.md / CODE_LAW.md)", code_law,
            ))
        if self._memory_prompt:
            refs.append(untrusted_reference("workspace long-term memory", self._memory_prompt))
        for block in self._runtime_system_blocks:
            refs.append(untrusted_reference("runtime or hook context", block))
        return refs

    def set_memory_prompt(self, prompt: str) -> None:
        """Recall results for the current turn; refreshed every turn."""
        self._memory_prompt = prompt or ""

    def set_mcp_tools_prompt(self, prompt: str) -> None:
        """更新 MCP 工具提示（无需清缓存 — Late Binding 自动感知）。"""
        self._mcp_tools_prompt = prompt or ""

    def set_skills_prompt(self, prompt: str) -> None:
        """更新 Skills 提示（无需清缓存）。"""
        self._skills_prompt = prompt or ""

    def set_output_style_prompt(self, prompt: str) -> None:
        """更新输出风格提示。传入空字符串 = default（不注入）。"""
        self._output_style_prompt = prompt or ""

    def set_runtime_system_blocks(self, blocks: List[str]) -> None:
        """Runtime/plugin notifications are untrusted user-role references."""
        self._runtime_system_blocks = [
            str(block).strip() for block in (blocks or []) if str(block).strip()
        ]

    # ══════════════════════════════════════════════════════════════
    # Internal: system message assembly
    # ══════════════════════════════════════════════════════════════

    def _build_system_messages(self) -> List[Dict[str, Any]]:
        """实时组装所有 system messages。"""
        messages: List[Dict[str, Any]] = []

        # L1: System prompt + usage_notes (tool prompts from live registry)
        l1 = self._build_l1()
        if l1:
            messages.append({"role": "system", "content": l1})

        # Only trusted application text can be system-level. All external
        # reference material is injected by get_reference_messages() as data.
        messages.append({"role": "system", "content": TRUST_BOUNDARY_RULES})

        return messages

    def _tool_usage_notes(self) -> str:
        """Collect live tool advice exclusively for low-trust references."""
        try:
            visible = getattr(self.tool_registry, "get_prompt_tools", None)
            tools = visible() if callable(visible) else self.tool_registry.get_all_tools()
        except Exception:
            tools = []
        lines: List[str] = []
        for tool in tools:
            notes = getattr(tool, "usage_notes", "")
            if isinstance(notes, str) and notes.strip():
                lines.append(notes.strip())
        disabled = self._get_disabled_tools()
        if disabled:
            lines.append("Disabled tools (temporary): " + ", ".join(str(n) for n in disabled))
        return "\n".join(lines)

    def _build_l1(self) -> str:
        """Only bundled static instructions (or trusted operator override).

        Dynamic tool descriptions and output-style strings belong in the
        untrusted reference section, never inside a system message.
        """
        l1_text = self._load_l1_text()
        l1_text = l1_text.replace("{tools}", "")
        l1_text = l1_text.replace("{output_style}", "")
        return self._append_plan_mode_guidance(l1_text).strip()

    # ══════════════════════════════════════════════════════════════
    # Lightweight caches
    # ══════════════════════════════════════════════════════════════

    def _load_l1_text(self) -> str:
        """Read the installed Markdown L1 prompt as data, never execute Python.

        Only trusted framework/bootstrap code may supply trusted_prompt_root,
        which is useful for fixture-based tests. Workspace-controlled prompt
        files have no authority unless deliberately installed by the operator.
        """
        if self.system_prompt_override:
            return self.system_prompt_override

        root = (Path(self.trusted_prompt_root).resolve()
                if self.trusted_prompt_root is not None
                else Path(__file__).resolve().parents[2])
        l1_path = root / "prompts" / "agents_prompts" / "L1_system_prompt.md"
        try:
            st = l1_path.stat()
            if st.st_size > 200_000 or not l1_path.is_file():
                return ""
            if self._cached_l1_mtime == st.st_mtime_ns and self._cached_l1_text:
                return self._cached_l1_text
            prompt = l1_path.read_text(encoding="utf-8")
            if len(prompt) > 200_000:
                return ""
        except (OSError, UnicodeError):
            return ""
        self._cached_l1_mtime = st.st_mtime_ns
        self._cached_l1_text = prompt
        return prompt

    def _load_instructions(self) -> str:
        """Load AGENTS.md / CLAUDE.md / CODE_LAW.md (signature-cached)."""
        signature = instruction_signature(self.project_root)
        if signature == self._cached_instruction_signature and self._cached_instructions:
            return self._cached_instructions
        self._cached_instruction_signature = signature
        self._cached_instructions = load_instructions(self.project_root)
        return self._cached_instructions

    # ══════════════════════════════════════════════════════════════
    # Helpers
    # ══════════════════════════════════════════════════════════════

    def _get_disabled_tools(self) -> List[str]:
        if hasattr(self.tool_registry, "get_disabled_tools"):
            try:
                return self.tool_registry.get_disabled_tools()
            except Exception:
                pass
        return []

    def _append_plan_mode_guidance(self, prompt: str) -> str:
        """Append plan mode usage guidance if EnterPlanMode is registered.

        The block documents an available tool, so it must never read as the
        session's current state: a model told it is already in plan mode
        reports itself as read-only while it can still write.
        """
        try:
            tools = self.tool_registry.get_all_tools()
            has_enter = any(getattr(t, "name", "") == "EnterPlanMode" for t in tools)
        except Exception:
            has_enter = False

        if not has_enter:
            return prompt

        guidance = (
            "\n\n# Plan Mode\n"
            "EnterPlanMode is an available tool, not a description of your "
            "current state: plan mode is not active unless you call it, and this "
            "session stays read/write until then.  For complex or multi-file "
            "changes, call EnterPlanMode first — it switches the session to "
            "read-only so you can analyse the codebase and produce a structured "
            "plan before editing.  Call ExitPlanMode with that plan when you are "
            "ready to execute.\n"
        )
        return prompt + guidance
