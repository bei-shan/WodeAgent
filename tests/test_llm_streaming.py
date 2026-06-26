"""LLM streaming contract tests."""

from __future__ import annotations

from types import SimpleNamespace

from core.llm import HelloAgentsLLM
from core.response_parser import extract_content, extract_reasoning_content, extract_tool_calls, extract_usage


class _StreamingCompletions:
    def __init__(self, recorder: dict, chunks, fail_stream_options: bool = False):
        self._recorder = recorder
        self._chunks = chunks
        self._fail_stream_options = fail_stream_options
        self.calls = 0

    def create(self, **kwargs):
        self.calls += 1
        self._recorder.setdefault("calls", []).append(dict(kwargs))
        self._recorder.clear()
        self._recorder["calls"] = self._recorder.get("calls", [])
        self._recorder.update(kwargs)
        if self._fail_stream_options and kwargs.get("stream_options") is not None:
            raise RuntimeError("stream_options is not supported")
        return iter(self._chunks)


class _StreamingClient:
    def __init__(self, completions: _StreamingCompletions):
        self.chat = SimpleNamespace(completions=completions)


def _choice(delta=None, finish_reason=None):
    return SimpleNamespace(delta=delta or SimpleNamespace(), finish_reason=finish_reason)


def _chunk(delta=None, finish_reason=None, usage=None):
    choices = [] if delta is None and finish_reason is None else [_choice(delta, finish_reason)]
    return SimpleNamespace(choices=choices, usage=usage)


def _tool_delta(index=0, call_id=None, name=None, arguments=None):
    fn = SimpleNamespace(name=name, arguments=arguments)
    return SimpleNamespace(index=index, id=call_id, type="function", function=fn)


def _install_client(monkeypatch, recorder: dict, chunks, fail_stream_options: bool = False):
    completions = _StreamingCompletions(recorder, chunks, fail_stream_options=fail_stream_options)
    monkeypatch.setattr(HelloAgentsLLM, "_create_client", lambda self: _StreamingClient(completions))
    return completions


def _llm() -> HelloAgentsLLM:
    return HelloAgentsLLM(
        model="deepseek-chat",
        provider="deepseek",
        api_key="test-key",
        base_url="https://api.deepseek.com",
        temperature=0.3,
    )


def test_stream_raw_emits_content_and_merges_response(monkeypatch):
    recorder = {}
    _install_client(
        monkeypatch,
        recorder,
        [
            _chunk(SimpleNamespace(content="hello")),
            _chunk(SimpleNamespace(content=" world"), finish_reason="stop"),
            _chunk(usage=SimpleNamespace(prompt_tokens=2, completion_tokens=3, total_tokens=5)),
        ],
    )
    llm = _llm()
    events = []

    raw = llm.stream_raw(
        [{"role": "user", "content": "hi"}],
        tools=[{"type": "function", "function": {"name": "Ping"}}],
        tool_choice="auto",
        on_delta=lambda event_type, text: events.append((event_type, text)),
    )

    assert recorder["stream"] is True
    assert recorder["stream_options"] == {"include_usage": True}
    assert recorder["tools"][0]["function"]["name"] == "Ping"
    assert recorder["tool_choice"] == "auto"
    assert events == [("content", "hello"), ("content", " world")]
    assert extract_content(raw) == "hello world"
    assert extract_usage(raw)["total_tokens"] == 5


def test_stream_raw_emits_reasoning_and_merges_tool_call(monkeypatch):
    recorder = {}
    _install_client(
        monkeypatch,
        recorder,
        [
            _chunk(SimpleNamespace(reasoning_content="think ")),
            _chunk(SimpleNamespace(reasoning_content="more")),
            _chunk(SimpleNamespace(tool_calls=[_tool_delta(index=0, call_id="call_1", name="Read", arguments='{"file') ])),
            _chunk(SimpleNamespace(tool_calls=[_tool_delta(index=0, arguments='_path":"a.py"}')]), finish_reason="tool_calls"),
        ],
    )
    llm = _llm()
    events = []

    raw = llm.stream_raw(
        [{"role": "user", "content": "read"}],
        on_delta=lambda event_type, text: events.append((event_type, text)),
    )

    assert events == [("reasoning", "think "), ("reasoning", "more")]
    assert extract_reasoning_content(raw) == "think more"
    calls = extract_tool_calls(raw)
    assert calls == [{"id": "call_1", "name": "Read", "arguments": '{"file_path":"a.py"}'}]


def test_stream_raw_retries_without_stream_options_when_provider_rejects(monkeypatch):
    recorder = {}
    completions = _install_client(
        monkeypatch,
        recorder,
        [_chunk(SimpleNamespace(content="ok"), finish_reason="stop")],
        fail_stream_options=True,
    )
    llm = _llm()

    raw = llm.stream_raw([{"role": "user", "content": "hi"}])

    assert completions.calls == 2
    assert recorder["stream"] is True
    assert "stream_options" not in recorder
    assert extract_content(raw) == "ok"


def test_think_and_stream_invoke_remain_text_iterators(monkeypatch):
    recorder = {}
    _install_client(
        monkeypatch,
        recorder,
        [_chunk(SimpleNamespace(content="a")), _chunk(SimpleNamespace(content="b"))],
    )
    llm = _llm()

    assert list(llm.think([{"role": "user", "content": "hi"}])) == ["a", "b"]
    assert list(llm.stream_invoke([{"role": "user", "content": "hi"}], tool_choice="none")) == ["a", "b"]
    assert recorder["tool_choice"] == "none"
