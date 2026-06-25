"""Context builder for ReAct prompt assembly — Late Binding (Pi-inspired).

Each ``build_messages()`` call assembles the full message list from
live data sources (tool registry, MCP status, skills, CODE_LAW).
No full-system-message cache — only cheap individual caches (L1 text,
CODE_LAW mtime, L1 file mtime) that never need manual invalidation.

Messages 格式：
[
  {"role": "system", "content": "L1 系统提示 + 工具 usage_notes"},
  {"role": "system", "content": "L2: CODE_LAW.md（如有）"},
  {"role": "user", "content": "...问题..."},
  {"role": "assistant", "content": "...", "tool_calls": [...]},
  {"role": "tool", "tool_call_id": "...", "content": "{截断后JSON}"},
  ...
]
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import runpy
from typing import List, Optional, Dict, Any


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
    mcp_tools_prompt: Optional[str] = None
    skills_prompt: Optional[str] = None

    # ── Lightweight caches (self-invalidating) ──
    _cached_l1_mtime: Optional[float] = field(default=None, init=False)
    _cached_l1_text: str = field(default="", init=False)
    _cached_code_law: str = field(default="", init=False)
    _cached_code_law_mtime: Optional[float] = field(default=None, init=False)

    # ── Live state (no cache invalidation needed) ──
    _runtime_system_blocks: List[str] = field(default_factory=list, init=False)
    _output_style_prompt: str = field(default="", init=False)
    _mcp_tools_prompt: str = field(default="", init=False)
    _skills_prompt: str = field(default="", init=False)

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
        messages.extend(history_messages)
        return messages

    def get_system_messages(self) -> List[Dict[str, Any]]:
        """获取 system messages（供日志/快照使用）。"""
        return self._build_system_messages()

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
        """设置 runtime 通知块（注入 system，不污染 user 轮次）。"""
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

        # MCP tools prompt (live — MCPFeature updates this)
        if self._mcp_tools_prompt:
            messages.append({"role": "system", "content": self._mcp_tools_prompt})

        # L2: CODE_LAW (mtime-cached)
        code_law = self._load_code_law()
        if code_law:
            messages.append({"role": "system",
                             "content": f"# Project Rules (CODE_LAW)\n{code_law}"})

        # Runtime blocks (plan mode, team status, hook messages, etc.)
        for block in self._runtime_system_blocks:
            messages.append({"role": "system", "content": block})

        return messages

    def _build_l1(self) -> str:
        """Build L1 system prompt: base text + usage_notes + disabled tools.

        The base L1 text is mtime-cached (file change → reload).
        usage_notes come from the live tool registry (always fresh).
        """
        # L1 base text (mtime-cached)
        l1_text = self._load_l1_text()

        # Tool usage notes from live registry (pure memory, always fresh)
        usage_lines: List[str] = []
        try:
            tools = self.tool_registry.get_all_tools()
        except Exception:
            tools = []

        for tool in tools:
            notes = getattr(tool, "usage_notes", "")
            if notes and notes.strip():
                usage_lines.append(notes.strip())

        # Skills prompt (live)
        if self._skills_prompt:
            usage_lines.append(self._skills_prompt.strip())

        # Disabled tools (live — circuit breaker state)
        disabled = self._get_disabled_tools()
        if disabled:
            usage_lines.append("## Disabled Tools (temporary)")
            for name in disabled:
                usage_lines.append(f"- {name}")

        # Replace {tools} placeholder or append
        tools_text = "\n".join(usage_lines)
        if "{tools}" in l1_text:
            l1_text = l1_text.replace("{tools}", tools_text)

        # Replace {output_style} placeholder
        l1_text = l1_text.replace("{output_style}", self._output_style_prompt)

        # Plan mode guidance (conditional)
        l1_text = self._append_plan_mode_guidance(l1_text)

        return l1_text.strip()

    # ══════════════════════════════════════════════════════════════
    # Lightweight caches
    # ══════════════════════════════════════════════════════════════

    def _load_l1_text(self) -> str:
        """Load L1 system prompt text (mtime-cached)."""
        if self.system_prompt_override:
            return self.system_prompt_override

        l1_path = Path(self.project_root) / "prompts" / "agents_prompts" / "L1_system_prompt.py"
        if not l1_path.exists():
            return ""

        try:
            mtime = l1_path.stat().st_mtime
        except OSError:
            return ""

        # mtime cache hit
        if self._cached_l1_mtime == mtime and self._cached_l1_text:
            return self._cached_l1_text

        data = runpy.run_path(str(l1_path))
        prompt = data.get("system_prompt", "")
        if not isinstance(prompt, str):
            return ""

        self._cached_l1_mtime = mtime
        self._cached_l1_text = prompt
        return prompt

    def _load_code_law(self) -> str:
        """Load CODE_LAW.md (mtime-cached)."""
        for filename in ("code_law.md", "CODE_LAW.md"):
            code_law_path = Path(self.project_root) / filename
            if not code_law_path.exists():
                continue
            try:
                mtime = code_law_path.stat().st_mtime
            except OSError:
                return ""
            if self._cached_code_law_mtime == mtime and self._cached_code_law:
                return self._cached_code_law
            try:
                self._cached_code_law = code_law_path.read_text(encoding="utf-8")
            except OSError:
                self._cached_code_law = ""
            self._cached_code_law_mtime = mtime
            return self._cached_code_law
        return ""

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
        """Append plan mode usage guidance if EnterPlanMode is registered."""
        try:
            tools = self.tool_registry.get_all_tools()
            has_enter = any(getattr(t, "name", "") == "EnterPlanMode" for t in tools)
        except Exception:
            has_enter = False

        if not has_enter:
            return prompt

        guidance = (
            "\n\n# Plan Mode\n"
            "For complex or multi-file changes, use EnterPlanMode to analyse "
            "the codebase first and produce a structured plan.  This avoids "
            "premature edits and lets you gather all context before writing code.  "
            "Call ExitPlanMode with your plan when ready to execute.\n"
        )
        return prompt + guidance
