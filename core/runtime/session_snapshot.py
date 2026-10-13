"""Session snapshot assembly is independent of the CodeAgent orchestrator.

SessionSnapshotBuilder owns the v2 persistence schema projection and is shared
by automatic saves and legacy save_session(path). It does not own live
history, the session catalog or Agent's run loop.
"""
from __future__ import annotations

from typing import Any, Protocol

from core.session_store import build_session_snapshot


class SnapshotHost(Protocol):
    project_root: str
    context_builder: Any
    history_manager: Any
    tool_registry: Any
    team_manager: Any
    team_store_dir: str
    task_store_dir: str
    _active_worktree: Any
    _skills_prompt: str
    _mcp_tools_prompt: str

    def _get_system_messages_for_run(self) -> list[dict]: ...
    def _get_openai_tools_for_current_mode(self) -> list[dict]: ...


class SessionSnapshotBuilder:
    def __init__(self, host: SnapshotHost):
        self.host = host

    def capture(self) -> dict:
        """Build a session snapshot dict for the current state."""
        host = self.host
        system_messages = host._get_system_messages_for_run()
        history_messages = host.history_manager.serialize_messages()
        tool_schema = host._get_openai_tools_for_current_mode()
        teams_snapshot = host.team_manager.export_state() if host.team_manager else {}
        worktree_state = None
        if host._active_worktree:
            worktree_state = {
                "name": host._active_worktree.get("name"),
                "path": host._active_worktree.get("path"),
                "branch": host._active_worktree.get("branch"),
            }
        return build_session_snapshot(
            system_messages=system_messages,
            history_messages=history_messages,
            tool_schema=tool_schema,
            project_root=host.project_root,
            cwd=".",
            code_law_text=host.context_builder._load_code_law(),
            skills_prompt=host._skills_prompt,
            mcp_tools_prompt=host._mcp_tools_prompt,
            read_cache=host.tool_registry.export_read_cache(),
            tool_output_dir="tool-output",
            schema_version=2,
            teams_snapshot=teams_snapshot,
            parallel_work_index=(teams_snapshot.get("work_items", {}) if isinstance(teams_snapshot, dict) else {}),
            team_store_dir=host.team_store_dir,
            task_store_dir=host.task_store_dir,
            worktree_state=worktree_state,
            # ── v2 tree fields ──
            cursor_id=host.history_manager.get_cursor_id(),
            history_entries=host.history_manager.serialize_entries(),
            labels=host.history_manager._labels,
            current_model=host.history_manager.get_current_model(),
            thinking_level=host.history_manager.get_thinking_level(),
        )

