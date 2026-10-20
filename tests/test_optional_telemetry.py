"""Telemetry is optional, content-free, correlated, and never blocks execution."""
from __future__ import annotations

import logging
from types import SimpleNamespace

import pytest

from core.runtime import telemetry as telemetry_module
from core.runtime.model_gateway import ModelGateway
from core.runtime.tool_runtime import ToolRuntime
from core.runtime.turn_lifecycle import AgentTurnLifecycle


class DummyHost:
    def __init__(self):
        from threading import Event
        self.logger = logging.getLogger("telemetry-test")
        self.console_progress = False
        self.console_verbose = False
        self._skills_prompt = ""
        self._run_id = 0
        self._run_limit_hit = False
        self._interrupt_requested = Event()
        self.context_builder = SimpleNamespace(set_skills_prompt=lambda *_: None)
        self.trace_logger = SimpleNamespace(log_event=lambda *_ , **__: None)
        self.history_manager = SimpleNamespace(
            append_user=lambda *_: None, get_message_count=lambda: 1,
            get_rounds_count=lambda: 1,
        )
        self.event_sink = SimpleNamespace(emit=lambda *_: None)

    def _refresh_skills_prompt(self): pass
    def _log_system_messages_if_needed(self, *_): pass
    def _log_message_write(self, *_, **__): pass
    def _auto_save_session(self): pass
    def _console(self, *_): pass

    def _react_loop(self, pending_input, show_raw, trace_logger):
        class Model:
            model = "gpt-6"
            def invoke_raw(self, *_args, **_kwargs):
                return {"usage": {
                    "prompt_tokens": 12, "completion_tokens": 5,
                    "total_tokens": 17,
                }}
        ModelGateway(Model(), streaming=False).invoke(
            messages=[{"role": "user", "content": pending_input}],
            tools_schema=[], tool_choice="auto", interceptors=[],
            agent=self, on_delta=lambda *_: None,
        )
        class Tool:
            def execute_tool(self, name, parameters):
                assert parameters["input"] == "SECRET_FILE_CONTENT"
                return '{"status":"success","text":"PRIVATE_TOOL_OUTPUT"}'
        ToolRuntime(Tool(), self.logger).execute(
            "Read", {"input": "SECRET_FILE_CONTENT"},
            agent=self, interceptors=[], emit_messages=lambda *_: None,
            record_timing=lambda *_: None,
        )
        return "PRIVATE_MODEL_OUTPUT"


def test_default_off_loads_no_backend(monkeypatch):
    monkeypatch.delenv("AGENT_TELEMETRY_BACKEND", raising=False)
    assert telemetry_module.get_telemetry().backend == "off"
    host = DummyHost()
    assert AgentTurnLifecycle(host).run("SECRET_PROMPT") == "PRIVATE_MODEL_OUTPUT"


def test_otel_span_hierarchy_and_strict_content_exclusion(monkeypatch):
    sdk = pytest.importorskip("opentelemetry.sdk.trace")
    export = pytest.importorskip("opentelemetry.sdk.trace.export")
    provider = sdk.TracerProvider()
    sink = export.InMemorySpanExporter() if hasattr(export, "InMemorySpanExporter") else None
    if sink is None:
        from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
        sink = InMemorySpanExporter()
    provider.add_span_processor(export.SimpleSpanProcessor(sink))
    telemetry = telemetry_module.Telemetry("otlp", provider.get_tracer("ridge-test"))
    monkeypatch.setattr(telemetry_module, "get_telemetry", lambda: telemetry)
    host = DummyHost()
    assert AgentTurnLifecycle(host).run("SECRET_PROMPT") == "PRIVATE_MODEL_OUTPUT"
    spans = {span.name: span for span in sink.get_finished_spans()}
    assert set(spans) == {"agent.turn", "agent.model", "agent.tool"}
    root = spans["agent.turn"]
    assert spans["agent.model"].parent.span_id == root.context.span_id
    assert spans["agent.tool"].parent.span_id == root.context.span_id
    assert spans["agent.model"].attributes["gen_ai.usage.input_tokens"] == 12
    assert spans["agent.model"].attributes["gen_ai.usage.output_tokens"] == 5
    assert spans["agent.tool"].attributes["ridge.tool.name"] == "Read"
    assert spans["agent.tool"].attributes["ridge.tool.status"] == "success"
    assert root.attributes["ridge.turn.status"] == "completed"
    all_attrs = repr([(span.name, dict(span.attributes), span.events) for span in spans.values()])
    for secret in ("SECRET_PROMPT", "SECRET_FILE_CONTENT", "PRIVATE_TOOL_OUTPUT", "PRIVATE_MODEL_OUTPUT"):
        assert secret not in all_attrs
    provider.shutdown()


class FakeObservation:
    def __init__(self):
        self.updates = []
    def update(self, **kwargs):
        self.updates.append(kwargs)


class FakeClient:
    def __init__(self):
        self.started = []
        self.exits = []
    def start_as_current_observation(self, **kwargs):
        self.started.append(kwargs)
        outer = self
        obj = FakeObservation()
        class Context:
            def __enter__(self): return obj
            def __exit__(self, *exc):
                outer.exits.append(exc)
        return Context()


def test_langfuse_v4_generation_metadata_usage_and_no_contents(monkeypatch):
    client = FakeClient()
    telemetry = telemetry_module.Telemetry("langfuse", client)
    monkeypatch.setattr(telemetry_module, "get_telemetry", lambda: telemetry)
    assert AgentTurnLifecycle(DummyHost()).run("SECRET_PROMPT") == "PRIVATE_MODEL_OUTPUT"
    assert [item["as_type"] for item in client.started] == ["agent", "generation", "tool"]
    assert client.started[1]["model"] == "gpt-6"
    assert len(client.exits) == 3
    all_items = repr(client.started)
    assert "SECRET_PROMPT" not in all_items
    assert "SECRET_FILE_CONTENT" not in all_items


def test_untrusted_attrs_and_failure_content_are_not_exported():
    client = FakeClient()
    telemetry = telemetry_module.Telemetry("langfuse", client)
    with pytest.raises(RuntimeError, match="PRIVATE_EXCEPTION"):
        with telemetry.span("agent.tool", kind="tool") as obs:
            obs.attribute("prompt", "SECRET_PROMPT")
            obs.attribute("ridge.tool.name", "Read")
            raise RuntimeError("PRIVATE_EXCEPTION")
    assert client.exits == [(None, None, None)]  # no exception forwarded into SDK


def test_failed_exporter_does_not_change_agent_result(monkeypatch):
    class Broken:
        def start_as_current_observation(self, **kwargs):
            raise RuntimeError("exporter down with SECRET")
    monkeypatch.setattr(
        telemetry_module, "get_telemetry",
        lambda: telemetry_module.Telemetry("langfuse", Broken()),
    )
    assert AgentTurnLifecycle(DummyHost()).run("SECRET_PROMPT") == "PRIVATE_MODEL_OUTPUT"


def test_off_backend_must_not_import_sdk(monkeypatch):
    monkeypatch.setenv("AGENT_TELEMETRY_BACKEND", "off")
    monkeypatch.delenv("LANGFUSE_PUBLIC_KEY", raising=False)
    assert telemetry_module.get_telemetry().backend == "off"
    with telemetry_module.get_telemetry().span("test") as span:
        span.usage({"prompt_tokens": 1})
        span.attribute("ridge.tool.name", "Read")
