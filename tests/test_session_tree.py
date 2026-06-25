"""Tests for session tree: Message tree model, HistoryManager fork, JSONL store."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest

from core.message import Message, generate_short_id
from core.config import Config
from core.context_engine.history_manager import (
    HistoryManager,
    ENTRY_MESSAGE, ENTRY_LEAF, ENTRY_MODEL_CHANGE,
    ENTRY_THINKING_CHANGE, ENTRY_BRANCH_SUMMARY,
)
from core.context_engine.jsonl_store import JsonlSessionStore


# ══════════════════════════════════════════════════════════════
# Message tree model
# ══════════════════════════════════════════════════════════════

class TestMessageTree:
    def test_message_has_id_and_parent(self):
        msg = Message(content="hello", role="user")
        assert msg.message_id
        assert len(msg.message_id) == 8
        assert msg.parent_id is None

    def test_message_with_parent(self):
        root = Message(content="root", role="user")
        child = Message(content="child", role="assistant", parent_id=root.message_id)
        assert child.parent_id == root.message_id

    def test_generate_short_id_unique(self):
        ids = {generate_short_id() for _ in range(100)}
        assert len(ids) == 100

    def test_to_entry(self):
        msg = Message(content="hello", role="user", parent_id="abc123")
        entry = msg.to_entry()
        assert entry["type"] == "message"
        assert entry["id"] == msg.message_id
        assert entry["parentId"] == "abc123"
        assert entry["role"] == "user"
        assert entry["content"] == "hello"

    def test_backward_compat_no_id(self):
        """Messages created without explicit ID still get auto-generated IDs."""
        msg = Message(content="hello", role="user")
        assert msg.message_id
        assert msg.parent_id is None


# ══════════════════════════════════════════════════════════════
# HistoryManager tree operations
# ══════════════════════════════════════════════════════════════

class TestHistoryManagerTree:
    def test_messages_have_parent_ids(self):
        hm = HistoryManager()
        u = hm.append_user("hello")
        a = hm.append_assistant("hi")
        assert u.parent_id is None  # first message = root
        assert a.parent_id == u.message_id

    def test_get_current_branch_linear(self):
        hm = HistoryManager()
        hm.append_user("u1")
        hm.append_assistant("a1")
        hm.append_tool("LS", "{}")
        hm.append_assistant("done")

        branch = hm.get_current_branch()
        assert len(branch) == 4

    def test_fork_creates_new_branch(self):
        hm = HistoryManager()
        root = hm.append_user("task")
        hm.append_assistant("working")
        hm.append_tool("Read", "{}")
        branch_point = hm.append_assistant("result 1")

        # Fork back to root
        hm.fork(root.message_id)
        hm.append_assistant("result 2 (different approach)")

        # Current branch should only have root → result 2
        branch = hm.get_current_branch()
        assert len(branch) == 2
        assert branch[-1].content == "result 2 (different approach)"

    def test_fork_invalid_id_raises(self):
        hm = HistoryManager()
        with pytest.raises(ValueError):
            hm.fork("nonexistent")

    def test_navigate_to_with_summarize(self):
        hm = HistoryManager(summary_generator=lambda msgs: "branch summary")
        root = hm.append_user("task")
        hm.append_assistant("work 1")
        hm.append_tool("Read", "{}")
        hm.append_assistant("done 1")

        # Navigate back to root with summarize
        hm.navigate_to(root.message_id, summarize=True)
        hm.append_assistant("done 2")

        branch = hm.get_current_branch()
        assert len(branch) == 3  # root, branch_summary, done 2

    def test_append_model_change(self):
        hm = HistoryManager()
        hm.append_user("hi")
        mid = hm.append_model_change("openai", "gpt-4o")
        hm.append_assistant("response")

        assert hm.get_current_model() == {"provider": "openai", "modelId": "gpt-4o"}
        # Assistant should have model metadata
        msgs = hm.get_messages()
        last_assistant = [m for m in msgs if m.role == "assistant"][-1]
        assert last_assistant.metadata.get("model") == "gpt-4o"

    def test_append_thinking_change(self):
        hm = HistoryManager()
        hm.append_user("hi")
        hm.append_thinking_change("on")
        assert hm.get_thinking_level() == "on"

    def test_get_tree_structure(self):
        hm = HistoryManager()
        hm.append_user("task")
        hm.append_assistant("work")
        hm.append_model_change("deepseek", "v4")

        tree = hm.get_tree()
        assert "nodes" in tree
        assert "children" in tree
        assert "cursor_id" in tree
        assert tree["cursor_id"] is not None

    def test_get_branches(self):
        hm = HistoryManager()
        root = hm.append_user("task")
        hm.append_assistant("branch A")
        hm.fork(root.message_id)
        hm.append_assistant("branch B")

        branches = hm.get_branches()
        # root should have 2 children
        root_branches = [b for b in branches if b["id"] == root.message_id]
        if root_branches:
            assert root_branches[0]["children_count"] == 2

    def test_serialize_and_load_with_tree_data(self):
        hm = HistoryManager()
        hm.append_user("u1")
        hm.append_assistant("a1")
        hm.append_model_change("deepseek", "chat")

        serialized = hm.serialize_messages()
        assert len(serialized) == 2
        assert "message_id" in serialized[0]
        assert "parent_id" in serialized[0]

        # Load into new HM
        hm2 = HistoryManager()
        hm2.load_messages(serialized)
        assert hm2.get_message_count() == 2
        assert hm2.get_cursor_id() is not None

    def test_serialize_and_load_entries(self):
        hm = HistoryManager()
        hm.append_user("u1")
        hm.append_assistant("a1")
        hm.append_model_change("deepseek", "chat")
        hm.append_thinking_change("on")

        entries = hm.serialize_entries()
        assert len(entries) >= 3  # 2 messages + model_change

        hm2 = HistoryManager()
        hm2.load_entries(entries)
        assert hm2.get_message_count() == 2
        assert hm2.get_current_model() == {"provider": "deepseek", "modelId": "chat"}
        assert hm2.get_thinking_level() == "on"

    def test_load_v1_messages_no_ids(self):
        """V1 snapshots without message_id/parent_id should still load."""
        v1_data = [
            {"role": "user", "content": "hello", "metadata": {}},
            {"role": "assistant", "content": "hi", "metadata": {}},
        ]
        hm = HistoryManager()
        hm.load_messages(v1_data)
        assert hm.get_message_count() == 2
        # Auto-generated IDs
        for msg in hm.get_messages():
            assert msg.message_id


# ══════════════════════════════════════════════════════════════
# JSONL Session Store
# ══════════════════════════════════════════════════════════════

class TestJsonlSessionStore:
    def test_create_and_append(self):
        with tempfile.TemporaryDirectory() as tmp:
            fp = Path(tmp) / "test.jsonl"
            store = JsonlSessionStore.create(fp, cwd="/tmp/test")
            assert store.metadata["cwd"] == "/tmp/test"
            assert store.entry_count == 0

            eid = store.append_entry({
                "type": "message", "role": "user", "content": "hello",
            })
            assert store.entry_count == 1
            assert store.get_leaf_id() == eid

    def test_open_and_read(self):
        with tempfile.TemporaryDirectory() as tmp:
            fp = Path(tmp) / "test.jsonl"
            store = JsonlSessionStore.create(fp)
            e1 = store.append_entry({"type": "message", "role": "user", "content": "hello"})
            e2 = store.append_entry({"type": "message", "role": "assistant", "content": "hi"})

            store2 = JsonlSessionStore.open(fp)
            assert store2.entry_count == 2
            assert store2.get_entry(e1) is not None
            assert store2.get_leaf_id() == e2

    def test_fork_and_branch(self):
        with tempfile.TemporaryDirectory() as tmp:
            fp = Path(tmp) / "test.jsonl"
            store = JsonlSessionStore.create(fp)
            root = store.append_entry({"type": "message", "role": "user", "content": "task"})
            store.append_entry({"type": "message", "role": "assistant", "content": "work"})

            # Fork back to root
            store.set_leaf(root)
            store.append_entry({"type": "message", "role": "user", "content": "new approach"})

            path = store.get_path_to_root(store.get_leaf_id())
            assert len(path) == 2
            assert path[0]["content"] == "task"
            assert path[1]["content"] == "new approach"

    def test_get_path_to_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            fp = Path(tmp) / "test.jsonl"
            store = JsonlSessionStore.create(fp)
            e1 = store.append_entry({"type": "message", "role": "user", "content": "root"})
            e2 = store.append_entry({"type": "message", "role": "assistant", "content": "child"})
            e3 = store.append_entry({"type": "message", "role": "assistant", "content": "grandchild"})

            path = store.get_path_to_root(e3)
            assert len(path) == 3
            assert path[0]["id"] == e1
            assert path[1]["id"] == e2
            assert path[2]["id"] == e3

    def test_labels(self):
        with tempfile.TemporaryDirectory() as tmp:
            fp = Path(tmp) / "test.jsonl"
            store = JsonlSessionStore.create(fp)
            e1 = store.append_entry({"type": "message", "role": "user", "content": "task"})
            store.append_entry({"type": "label", "targetId": e1, "label": "my label"})
            assert store.get_label(e1) == "my label"

    def test_empty_leaf_returns_empty_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            fp = Path(tmp) / "test.jsonl"
            store = JsonlSessionStore.create(fp)
            assert store.get_path_to_root(None) == []

    def test_set_leaf_invalid_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            fp = Path(tmp) / "test.jsonl"
            store = JsonlSessionStore.create(fp)
            with pytest.raises(ValueError):
                store.set_leaf("nonexistent")

    def test_find_entries(self):
        with tempfile.TemporaryDirectory() as tmp:
            fp = Path(tmp) / "test.jsonl"
            store = JsonlSessionStore.create(fp)
            store.append_entry({"type": "message", "role": "user", "content": "hello"})
            store.append_entry({"type": "message", "role": "assistant", "content": "hi"})
            store.append_entry({"type": "model_change", "provider": "openai", "modelId": "gpt4"})

            msgs = store.find_entries("message")
            assert len(msgs) == 2
            models = store.find_entries("model_change")
            assert len(models) == 1
