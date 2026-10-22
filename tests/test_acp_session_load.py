"""ACP session restore across process restarts (session/load)."""
from __future__ import annotations

import io
import json
import logging

import pytest

import agents.codeAgent as code_agent_module
import core.llm as llm_module
import tools.registry as registry_module
from agents.codeAgent import CodeAgent
from ridgecode.acp import AcpServer, default_agent_factory


class FakeAgent:
    restore_ok = True

    def __init__(self, reply="restored", **kwargs):
        self.reply = reply
        self.kwargs = kwargs
        self.event_sink = None
        self.prompts: list[str] = []
        self.closed = False
        self.restored: str | None = None

    def run(self, text: str) -> str:
        self.prompts.append(text)
        return self.reply

    def restore_session(self, session_id: str) -> bool:
        self.restored = session_id
        return self.restore_ok

    def close(self) -> None:
        self.closed = True


def _messages(out: io.StringIO) -> list[dict]:
    return [json.loads(line) for line in out.getvalue().splitlines() if line.strip()]


def _server(agent=None, *, resume=None):
    agent = agent or FakeAgent()
    out = io.StringIO()
    server = AcpServer(lambda cwd: agent, stdout=out, resume_factory=resume)
    return server, out


# ---------------------------------------------------------------------------
# ACP session/load
# ---------------------------------------------------------------------------
def test_load_restores_an_unknown_session_through_the_resume_factory():
    calls: list[tuple[str, str]] = []

    def resume(cwd: str, session_id: str):
        calls.append((cwd, session_id))
        return FakeAgent()

    server, out = _server(resume=resume)
    result = server.handle({
        "jsonrpc": "2.0", "id": 1, "method": "session/load",
        "params": {"sessionId": "abc123def456", "cwd": "/workspace", "mcpServers": []},
    })

    assert result == {}
    assert calls == [("/workspace", "abc123def456")]
    # The restored session is immediately promptable.
    prompt = server.handle({
        "jsonrpc": "2.0", "id": 2, "method": "session/prompt",
        "params": {"sessionId": "abc123def456", "prompt": [{"type": "text", "text": "hi"}]},
    })
    assert prompt == {"stopReason": "end_turn"}


def test_load_known_session_skips_the_resume_factory():
    def resume(cwd: str, session_id: str):  # pragma: no cover - must not run
        raise AssertionError("resume_factory must not be called for a live session")

    server, _ = _server(resume=resume)
    session_id = server.handle({
        "jsonrpc": "2.0", "id": 3, "method": "session/new", "params": {"cwd": "/tmp"},
    })["sessionId"]

    assert server.handle({
        "jsonrpc": "2.0", "id": 4, "method": "session/load",
        "params": {"sessionId": session_id, "cwd": "/tmp"},
    }) == {}


def test_load_rejects_unknown_or_restoreless_sessions():
    server, out = _server(resume=lambda cwd, sid: None)
    server.handle({
        "jsonrpc": "2.0", "id": 5, "method": "session/load",
        "params": {"sessionId": "ffffffffffff", "cwd": "/tmp"},
    })
    assert _messages(out)[-1]["error"]["code"] == -32602

    # No resume factory at all.
    bare, bare_out = _server()
    bare.handle({
        "jsonrpc": "2.0", "id": 6, "method": "session/load",
        "params": {"sessionId": "ffffffffffff", "cwd": "/tmp"},
    })
    assert _messages(bare_out)[-1]["error"]["code"] == -32602


def test_load_requires_cwd_before_touching_disk():
    calls: list[tuple[str, str]] = []

    def resume(cwd: str, session_id: str):
        calls.append((cwd, session_id))
        return FakeAgent()

    server, out = _server(resume=resume)
    server.handle({
        "jsonrpc": "2.0", "id": 7, "method": "session/load",
        "params": {"sessionId": "abc123def456"},
    })
    assert _messages(out)[-1]["error"]["code"] == -32602
    assert calls == []


def test_load_reports_restore_errors():
    def resume(cwd: str, session_id: str):
        raise RuntimeError("disk on fire")

    server, out = _server(resume=resume)
    server.handle({
        "jsonrpc": "2.0", "id": 8, "method": "session/load",
        "params": {"sessionId": "abc123def456", "cwd": "/tmp"},
    })
    error = _messages(out)[-1]["error"]
    assert error["code"] == -32603
    assert "restore failed" in error["message"]


# ---------------------------------------------------------------------------
# Default factory restoration
# ---------------------------------------------------------------------------
@pytest.fixture
def fake_agent_classes(monkeypatch):
    monkeypatch.setattr(code_agent_module, "CodeAgent", FakeAgent)
    monkeypatch.setattr(llm_module, "HelloAgentsLLM", lambda: "llm")
    monkeypatch.setattr(registry_module, "ToolRegistry", lambda: "registry")


def test_default_factory_restores_without_saving(fake_agent_classes):
    agent = default_agent_factory("/workspace", "abc123def456")
    assert isinstance(agent, FakeAgent)
    assert agent.restored == "abc123def456"
    assert agent.closed is False


def test_default_factory_returns_none_and_closes_when_restore_fails(fake_agent_classes, monkeypatch):
    monkeypatch.setattr(FakeAgent, "restore_ok", False)
    assert default_agent_factory("/workspace", "abc123def456") is None


def test_default_factory_without_session_does_not_restore(fake_agent_classes):
    agent = default_agent_factory("/workspace")
    assert agent.restored is None


# ---------------------------------------------------------------------------
# CodeAgent.restore_session contract
# ---------------------------------------------------------------------------
class _History:
    def __init__(self):
        self.loaded = None

    def load_entries(self, entries):
        self.loaded = ("entries", entries)

    def load_messages(self, messages):
        self.loaded = ("messages", messages)


class _Registry:
    def __init__(self):
        self.cache = None

    def import_read_cache(self, cache):
        self.cache = cache


class _Manager:
    def __init__(self, snapshot):
        self.snapshot = snapshot

    def load_session(self, session_id):
        return self.snapshot


class _FakeSelf:
    def __init__(self, snapshot):
        self._session_manager = _Manager(snapshot)
        self.history_manager = _History()
        self.tool_registry = _Registry()
        self.team_manager = None
        self.logger = logging.getLogger("restore-test")
        self._session_id = "old"
        self._system_messages_override = None

    def enter_worktree(self, path):  # pragma: no cover - not used in these cases
        self.worktree = path


def test_restore_session_delegates_to_apply_snapshot():
    snapshot = {"history_messages": []}

    class _Delegating(_FakeSelf):
        def _apply_snapshot(self, session_id, applied_snapshot):
            self.applied = (session_id, applied_snapshot)
            return True

    fake = _Delegating(snapshot)
    assert CodeAgent.restore_session(fake, "abc123def456") is True
    assert fake.applied == ("abc123def456", snapshot)


def test_apply_snapshot_restores_state_without_saving():
    snapshot = {
        "system_messages": [{"role": "system", "content": "prompt"}],
        "history_entries": [{"id": "m1", "role": "user", "content": "hi"}],
        "read_cache": {"a.py": {"file_mtime_ms": 1}},
        "teams_snapshot": {},
    }
    fake = _FakeSelf(snapshot)

    assert CodeAgent._apply_snapshot(fake, "abc123def456", snapshot) is True
    assert fake._session_id == "abc123def456"
    assert fake._system_messages_override == snapshot["system_messages"]
    assert fake.history_manager.loaded == ("entries", snapshot["history_entries"])
    assert fake.tool_registry.cache == snapshot["read_cache"]
    # _FakeSelf has no _auto_save_session: nothing persisted this restore.


def test_apply_snapshot_falls_back_to_flat_messages():
    snapshot = {"history_messages": [{"role": "user", "content": "legacy"}]}
    fake = _FakeSelf(snapshot)

    assert CodeAgent._apply_snapshot(fake, "abc123def456", snapshot) is True
    assert fake.history_manager.loaded == ("messages", snapshot["history_messages"])


def test_restore_session_returns_false_for_missing_snapshot():
    fake = _FakeSelf(None)
    assert CodeAgent.restore_session(fake, "abc123def456") is False
    assert fake._session_id == "old"
