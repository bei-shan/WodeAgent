"""Session tree branching must affect model prompts and survive process restarts."""
from __future__ import annotations

import logging
from types import SimpleNamespace

from core.context_engine.history_manager import HistoryManager
from core.runtime.context_runtime import ContextRuntime
from core.runtime.session_controller import SessionController
from core.session_manager import SessionManager
from core.session_store import build_session_snapshot
from tests.test_web_service import FakeAgent


def test_branch_context_never_contains_abandoned_sibling():
    history = HistoryManager()
    root = history.append_user("root task")
    history.append_assistant("ABANDONED PATH")
    history.fork(root.message_id)
    history.append_assistant("SELECTED PATH")
    assert "ABANDONED PATH" in str(history.to_messages_all())
    messages = history.to_messages_branch()
    assert "SELECTED PATH" in str(messages)
    assert "ABANDONED PATH" not in str(messages)

    class Trace:
        def log_event(self, *args, **kwargs): pass

    host = SimpleNamespace(
        history_manager=history,
        _build_messages=lambda rows: rows,
    )
    current, _ = ContextRuntime(host).build_step_messages(Trace(), 1)
    assert current == messages
    assert "ABANDONED PATH" not in str(current)


def test_tree_snapshot_restores_exact_selected_cursor_and_labels():
    history = HistoryManager()
    root = history.append_user("root")
    abandoned = history.append_assistant("old")
    history.append_label(root.message_id, "checkpoint")
    history.fork(root.message_id)
    selected = history.append_assistant("new")
    history.fork(root.message_id)
    serialized = history.serialize_entries()
    saved_cursor = history.get_cursor_id()
    assert saved_cursor == root.message_id
    assert saved_cursor != serialized[-1]["id"]

    restored = HistoryManager()
    restored.load_entries(serialized)
    restored.restore_tree_state(saved_cursor, history._labels)
    assert restored.get_cursor_id() == root.message_id
    assert restored.get_tree()["labels"][root.message_id] == "checkpoint"
    resumed = restored.append_assistant("resumed here")
    assert resumed.parent_id == root.message_id
    assert "old" not in str(restored.to_messages_branch())
    assert "new" not in str(restored.to_messages_branch())
    assert len(restored.get_messages()) == 4


def test_real_session_activation_restores_snapshot_cursor(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    history = HistoryManager()
    root = history.append_user("opening")
    history.append_assistant("discard")
    history.fork(root.message_id)
    catalog = SessionManager()
    sid = catalog.create_session("saved tree")
    catalog.save_session(sid, build_session_snapshot(
        system_messages=[], history_messages=history.serialize_messages(),
        tool_schema=[], project_root=str(tmp_path), schema_version=2,
        history_entries=history.serialize_entries(), cursor_id=root.message_id,
    ))
    class TreeAgent(FakeAgent):
        def __init__(self):
            super().__init__()
            self.history_manager = HistoryManager()
    controller = SessionController(workspace_base=str(tmp_path / "sessions"))
    session = controller.attach_session(sid, TreeAgent, title="saved tree")
    try:
        agent = session.ensure_agent()
        assert agent.history_manager.get_cursor_id() == root.message_id
        assert agent.history_manager.append_user("new branch").parent_id == root.message_id
    finally:
        controller.delete_session(sid)


def test_cycle_in_corrupt_snapshot_cannot_hang_branch_traversal():
    hm = HistoryManager()
    hm.load_entries([
        {"type": "message", "id": "aaaaaaaa", "parentId": "bbbbbbbb", "role": "user", "content": "a"},
        {"type": "message", "id": "bbbbbbbb", "parentId": "aaaaaaaa", "role": "assistant", "content": "b"},
    ])
    hm.restore_tree_state("aaaaaaaa")
    assert len(hm.get_current_branch()) == 2
