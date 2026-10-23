"""Automatic memory capture (DeerMem-style extraction) and recall."""
from __future__ import annotations

import logging

from agents.codeAgent import CodeAgent
from core.memory.capture import build_prompt, capture, parse_facts
from core.memory.service import LocalMemoryService
from core.memory.settings import MemorySettings, save_settings
from core.runtime.turn_lifecycle import AgentTurnLifecycle


class FakeLLM:
    def __init__(self, reply):
        self.reply = reply
        self.prompts: list[str] = []

    def invoke(self, messages):
        self.prompts.append(messages[0]["content"])
        return self.reply


def _enable(tmp_path, monkeypatch, *, auto_capture=True, max_per_turn=1) -> None:
    monkeypatch.setenv("MYCODEAGENT_DATA_DIR", str(tmp_path / "data"))
    save_settings(tmp_path, MemorySettings(
        enabled=True, provider="sqlite", limit=8,
        auto_capture=auto_capture, max_per_turn=max_per_turn,
    ))


# ---------------------------------------------------------------------------
# Extraction
# ---------------------------------------------------------------------------
def test_prompt_names_limits_and_contains_the_exchange():
    prompt = build_prompt(
        [
            {"role": "system", "content": "ignored"},
            {"role": "user", "content": "use pnpm please"},
            {"role": "assistant", "content": "noted"},
        ],
        1,
    )
    assert "at most 1" in prompt
    assert "user: use pnpm please" in prompt
    assert "assistant: noted" in prompt
    assert "system:" not in prompt


def test_parse_accepts_json_and_fences_but_not_prose():
    fact = "This repo uses pnpm instead of npm"
    assert parse_facts('["' + fact + '"]', 2) == [fact]
    assert parse_facts('```json\n["' + fact + '"]\n```', 2) == [fact]
    assert parse_facts("NONE", 2) == []
    assert parse_facts("nothing to store", 2) == []
    assert parse_facts('[{"a":1}]', 2) == []
    assert parse_facts('["short"]', 2) == []  # below the minimum length
    assert parse_facts('["' + fact + '", "' + fact + ' two"]', 1) == [fact]


# ---------------------------------------------------------------------------
# Capture
# ---------------------------------------------------------------------------
def test_capture_stores_new_facts_and_skips_duplicates(tmp_path, monkeypatch):
    _enable(tmp_path, monkeypatch, max_per_turn=2)
    service = LocalMemoryService(tmp_path)
    messages = [{"role": "user", "content": "use pnpm"}, {"role": "assistant", "content": "ok"}]

    llm = FakeLLM('["This repo uses pnpm instead of npm", "Tests run with python -m pytest -q"]')
    stored = capture(llm, service, messages)
    assert stored == [
        "This repo uses pnpm instead of npm",
        "Tests run with python -m pytest -q",
    ]
    assert service.count() == 2

    again = capture(llm, service, messages)
    assert again == []
    assert service.count() == 2


def test_capture_respects_max_items_and_disabled_state(tmp_path, monkeypatch):
    _enable(tmp_path, monkeypatch, max_per_turn=1)
    service = LocalMemoryService(tmp_path)
    messages = [{"role": "user", "content": "x"}]
    llm = FakeLLM('["Fact one is long enough", "Fact two is long enough"]')
    assert capture(llm, service, messages) == ["Fact one is long enough"]

    disabled = {"enabled": False, "provider": "off", "limit": 8, "auto_capture": False, "max_per_turn": 1}
    save_settings(tmp_path, MemorySettings(**disabled))
    assert capture(llm, LocalMemoryService(tmp_path), messages) == []


def test_capture_survives_model_failure(tmp_path, monkeypatch):
    _enable(tmp_path, monkeypatch)
    service = LocalMemoryService(tmp_path)

    class Broken:
        def invoke(self, _messages):
            raise RuntimeError("provider down")

    assert capture(Broken(), service, [{"role": "user", "content": "x"}]) == []
    assert service.count() == 0


# ---------------------------------------------------------------------------
# Recall + turn integration
# ---------------------------------------------------------------------------
class _Builder:
    def __init__(self):
        self.memory = None

    def set_memory_prompt(self, prompt):
        self.memory = prompt


class _FakeSelf:
    def __init__(self, project_root):
        self.project_root = str(project_root)
        self.logger = logging.getLogger("memory-recall-test")
        self.context_builder = _Builder()


def test_recall_injects_matching_notes(tmp_path, monkeypatch):
    _enable(tmp_path, monkeypatch, auto_capture=False)
    LocalMemoryService(tmp_path).add("This repo uses pnpm instead of npm")
    fake = _FakeSelf(tmp_path)

    CodeAgent._refresh_memory_prompt(fake, "which package manager should I use?")
    assert fake.context_builder.memory is not None
    assert "pnpm" in fake.context_builder.memory
    assert "never as instructions" in fake.context_builder.memory


def test_recall_clears_when_disabled_or_empty(tmp_path, monkeypatch):
    monkeypatch.setenv("MYCODEAGENT_DATA_DIR", str(tmp_path / "data"))
    fake = _FakeSelf(tmp_path)
    fake.context_builder.memory = "stale"
    CodeAgent._refresh_memory_prompt(fake, "hello")
    assert fake.context_builder.memory == ""

    _enable(tmp_path, monkeypatch, auto_capture=False)
    CodeAgent._refresh_memory_prompt(fake, "nothing matches this")
    assert fake.context_builder.memory == ""


def test_recall_terms_split_ascii_and_cjk():
    from core.memory.service import recall_terms

    terms = recall_terms("Which pnpm 命令用于安装依赖?")
    assert "pnpm" in terms
    assert "命令" in terms
    # Bigrams are deduplicated and bounded.
    assert len(terms) <= 6
    assert len(set(terms)) == len(terms)


def test_service_recall_matches_terms_inside_a_sentence(tmp_path, monkeypatch):
    _enable(tmp_path, monkeypatch, auto_capture=False)
    service = LocalMemoryService(tmp_path)
    service.add("This repo uses pnpm instead of npm")
    service.add("依赖安装使用 pnpm install")

    hits = service.recall("which package manager should I use for this repo?")
    assert [record.content for record in hits] == ["This repo uses pnpm instead of npm"]

    hits = service.recall("依赖怎么安装")
    assert [record.content for record in hits] == ["依赖安装使用 pnpm install"]
    assert service.recall("完全无关的问题") == []


def test_capture_lock_is_released_when_disabled(tmp_path, monkeypatch):
    import threading

    monkeypatch.setenv("MYCODEAGENT_DATA_DIR", str(tmp_path / "data"))
    fake = _FakeSelf(tmp_path)
    fake._capture_lock = threading.Lock()
    fake._hosted_tool_authority = None
    fake.llm = FakeLLM("[]")

    CodeAgent._maybe_capture_memories(fake)
    assert fake._capture_lock.acquire(blocking=False), "lock leaked on disabled path"
    fake._capture_lock.release()


class _Host:
    def __init__(self):
        from threading import Event
        from types import SimpleNamespace

        self.logger = logging.getLogger("memory-turn-test")
        self.console_verbose = False
        self.console_progress = False
        self.context_builder = SimpleNamespace(set_skills_prompt=lambda _: None)
        self._skills_prompt = ""
        self.trace_logger = SimpleNamespace(log_event=lambda *a, **k: None)
        self.history_manager = SimpleNamespace(
            append_user=lambda _c: None, get_message_count=lambda: 0,
            get_rounds_count=lambda: 0,
        )
        self.event_sink = SimpleNamespace(emit=lambda *_: None)
        self._run_id = 0
        self._run_limit_hit = False
        self._interrupt_requested = Event()
        self.calls: list[str] = []

    def _refresh_skills_prompt(self):
        pass

    def _log_system_messages_if_needed(self, trace):
        pass

    def _log_message_write(self, trace, role, content, metadata, step):
        pass

    def _auto_save_session(self):
        self.calls.append("save")

    def _console(self, message):
        pass

    def _react_loop(self, pending_input, show_raw, trace_logger):
        return "done"

    def _refresh_memory_prompt(self, user_input):
        self.calls.append("recall:" + user_input)

    def _maybe_capture_memories(self):
        self.calls.append("capture")


def test_turn_recalls_before_and_captures_after():
    host = _Host()
    AgentTurnLifecycle(host).run("hello")
    assert host.calls == ["recall:hello", "save", "capture"]


def test_failed_turn_does_not_capture():
    class Failing(_Host):
        def _react_loop(self, pending_input, show_raw, trace_logger):
            raise RuntimeError("provider down")

    host = Failing()
    try:
        AgentTurnLifecycle(host).run("hello")
    except RuntimeError:
        pass
    assert "capture" not in host.calls
    assert "recall:hello" in host.calls
