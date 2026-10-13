"""Branch compaction must neither leak sibling messages nor destroy tree nodes."""
from __future__ import annotations

from core.config import Config
from core.context_engine.history_manager import HistoryManager


def make_manager(summaries):
    def summarize(messages):
        sources = [(m.role, m.content) for m in messages]
        summaries.append(sources)
        return "SAFE SUMMARY " + " ".join(m.content for m in messages if m.role != "summary")
    return HistoryManager(
        config=Config(
            min_retain_rounds=1, context_window=64,
            compression_threshold=0.7, context_anchor_max_chars=100,
            summary_output_max_chars=10000,
        ),
        summary_generator=summarize,
    )


def test_compaction_uses_active_branch_and_retains_old_tree():
    seen = []
    hm = make_manager(seen)
    root = hm.append_user("task root")
    abandoned = hm.append_assistant("ABANDONED SECRET")
    hm.fork(root.message_id)
    selected = hm.append_assistant("selected answer")
    for i in range(4):
        hm.append_user(f"selected user {i}")
        hm.append_assistant(f"selected reply {i}")
    before = {m.message_id for m in hm.get_messages()}
    output = hm.compact(return_info=True)

    assert output["compressed"] is True
    assert output["branch_scoped"] is True
    assert seen
    assert all("ABANDONED SECRET" not in text for batch in seen for _, text in batch)
    assert "ABANDONED SECRET" not in str(hm.to_messages_branch())
    assert "SAFE SUMMARY" in str(hm.to_messages_branch())
    assert before == {m.message_id for m in hm.get_messages()}
    assert abandoned.message_id in hm.get_tree()["nodes"]
    assert selected.message_id in hm.get_tree()["nodes"]
    assert any(e.get("branchScoped") for e in hm.serialize_entries())


def test_compaction_overlay_survives_snapshot_and_old_fork_does_not_inherit_it():
    hm = make_manager([])
    root = hm.append_user("task root")
    original = hm.append_assistant("sibling secret")
    hm.fork(root.message_id)
    for i in range(4):
        hm.append_user(f"chosen user {i}")
        hm.append_assistant(f"chosen assistant {i}")
    assert hm.compact()
    saved = hm.serialize_entries()
    cursor = hm.get_cursor_id()
    projected = hm.to_messages_branch()

    restored = make_manager([])
    restored.load_entries(saved)
    restored.restore_tree_state(cursor, hm._labels)
    assert projected == restored.to_messages_branch()
    assert "sibling secret" not in str(restored.to_messages_branch())
    assert original.message_id in restored.get_tree()["nodes"]

    restored.fork(root.message_id)
    assert "SAFE SUMMARY" not in str(restored.to_messages_branch())
    next_message = restored.append_assistant("another path")
    assert next_message.parent_id == root.message_id
    assert "chosen assistant" not in str(restored.to_messages_branch())


def test_abandoned_long_sibling_does_not_trigger_selected_branch_compaction():
    hm = make_manager([])
    root = hm.append_user("short")
    hm.append_assistant("OLD SIBLING " * 2000)
    hm.fork(root.message_id)
    hm.append_assistant("new")
    assert hm.estimate_context_tokens("") < 64
    assert hm.should_compress("") is False


def test_repeated_branch_compaction_summarizes_previous_overlay_only():
    seen = []
    hm = make_manager(seen)
    root = hm.append_user("base")
    hm.append_assistant("discarded secret")
    hm.fork(root.message_id)
    for i in range(4):
        hm.append_user(f"active {i}")
        hm.append_assistant(f"answer {i}")
    assert hm.compact()
    for i in range(4, 8):
        hm.append_user(f"active {i}")
        hm.append_assistant(f"answer {i}")
    assert hm.compact()
    assert len(seen) == 2
    assert any(role == "summary" for role, _ in seen[1])
    assert all("discarded secret" not in c for batch in seen for _, c in batch)
    assert "discarded secret" not in str(hm.to_messages_branch())
