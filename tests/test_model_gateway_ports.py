"""ModelGateway preserves model streaming, interceptors and error behavior."""
import ast
from pathlib import Path

import pytest

from core.runtime.model_gateway import ModelGateway


class Provider:
    def __init__(self):
        self.calls = []
    def invoke_raw(self, messages, *, tools, tool_choice):
        self.calls.append(("invoke", messages, tools, tool_choice))
        return {"message": "plain"}
    def stream_raw(self, messages, *, tools, tool_choice, on_delta):
        self.calls.append(("stream", messages, tools, tool_choice))
        on_delta("content", "abc")
        return {"message": "streamed"}


class Interceptor:
    def __init__(self, name, log):
        self.name, self.log = name, log
    def llm_intercept(self, agent, messages, tools, choice, next_call):
        self.log.append("before:" + self.name)
        result = next_call()
        self.log.append("after:" + self.name)
        return result


def test_streaming_model_delivers_one_delta_and_intercept_order():
    provider = Provider()
    log, deltas = [], []
    features = (Interceptor("outer", log), Interceptor("inner", log))
    resp = ModelGateway(provider).invoke(
        messages=[{"role": "user", "content": "hi"}], tools_schema=[],
        tool_choice="auto", features=iter(features),
        agent=object(), on_delta=lambda kind, text: deltas.append((kind, text)),
    )
    assert resp["message"] == "streamed"
    assert deltas == [("content", "abc")]
    assert len(provider.calls) == 1 and provider.calls[0][0] == "stream"
    assert log == ["before:outer", "before:inner", "after:inner", "after:outer"]


def test_streaming_disabled_uses_single_nonstream_call():
    provider = Provider()
    result = ModelGateway(provider, streaming=False).invoke(
        messages=[], tools_schema=None, tool_choice="none", features=[],
        agent=object(), on_delta=lambda *_: None,
    )
    assert result["message"] == "plain"
    assert len(provider.calls) == 1 and provider.calls[0][0] == "invoke"


def test_provider_without_streaming_support_uses_invoke_raw():
    class Legacy:
        def invoke_raw(self, messages, *, tools, tool_choice):
            return "legacy"
    value = ModelGateway(Legacy()).invoke(
        messages=[], tools_schema=[], tool_choice="auto", features=[],
        agent=object(), on_delta=lambda *_: None,
    )
    assert value == "legacy"


def test_partial_stream_error_never_replays_nonstream():
    class Failing(Provider):
        def stream_raw(self, messages, *, tools, tool_choice, on_delta):
            on_delta("content", "partial")
            self.calls.append(("stream",))
            raise RuntimeError("disconnect")
    provider = Failing()
    seen = []
    with pytest.raises(RuntimeError, match="disconnect"):
        ModelGateway(provider).invoke(
            messages=[], tools_schema=[], tool_choice="auto", features=[],
            agent=object(), on_delta=lambda kind, text: seen.append(text),
        )
    assert seen == ["partial"] and provider.calls == [("stream",)]


def test_gateway_does_not_depend_on_codeagent_or_ui():
    src = (Path(__file__).resolve().parents[1] /
           "core/runtime/model_gateway.py").read_text(encoding="utf-8")
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.ImportFrom):
            assert not (node.module or "").startswith(("agents", "desktop", "tui"))
