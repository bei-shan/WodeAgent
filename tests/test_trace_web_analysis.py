"""Trace API is session-scoped, bounded, and never exposes private audit text."""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from fastapi.testclient import TestClient

from desktop.service.app import create_app
from desktop.service.application.trace_analysis import analyze_trace


def write_events(path: Path):
    events = [
        ("run_start", 0, {"run_id": 1, "input": "SECRET_PRIVATE_USER_PROMPT"}),
        ("model_call", 1, {
            "model": "test-model", "status": "success",
            "usage": {"prompt_tokens": 2000, "completion_tokens": 500, "total_tokens": 2500},
            "duration_ms": 450,
            "raw": "SECRET_MODEL_OUTPUT",
        }),
        ("model_output", 1, {"usage": {
            "prompt_tokens": 2000, "completion_tokens": 500, "total_tokens": 2500,
        }, "raw": "SECRET_MODEL_OUTPUT"}),
        ("tool_call", 1, {"tool": "Read", "tool_call_id": "call1", "args": {"content": "SECRET_TOOL_INPUT"}}),
        ("tool_result", 1, {"tool": "Read", "tool_call_id": "call1", "duration_ms": 55,
                            "result": {"status": "success", "text": "SECRET_FILE_CONTENT"}}),
        ("run_end", 0, {"run_id": 1, "status": "completed", "final": "SECRET_FINAL"}),
    ]
    with path.open("w", encoding="utf-8") as stream:
        for index, (event, step, payload) in enumerate(events):
            stream.write(json.dumps({
                "ts": f"2026-10-13T11:00:{index:02d}+00:00",
                "event": event, "step": step, "payload": payload,
            }, ensure_ascii=False) + "\n")


def test_safe_analysis_nested_nodes_cost_and_model_usage_are_not_double_counted(tmp_path, monkeypatch):
    path = tmp_path / "trace-example.jsonl"
    write_events(path)
    monkeypatch.setenv("TRACE_INPUT_USD_PER_MILLION", "1.5")
    monkeypatch.setenv("TRACE_OUTPUT_USD_PER_MILLION", "6")
    result = analyze_trace(path, "local-id")
    assert result["available"] is True
    run = result["runs"][0]
    assert len(run["steps"][0]["nodes"]) == 2
    assert run["tokens"] == {"input": 2000, "output": 500, "total": 2500}
    assert run["cost_usd_estimate"] == 0.006
    assert run["quality"]["label"] == "healthy"
    assert run["steps"][0]["nodes"][0]["duration_ms"] == 450
    assert run["duration_ms"] == 5000
    encoded = json.dumps(result)
    for secret in ("SECRET_PRIVATE_USER_PROMPT", "SECRET_MODEL_OUTPUT",
                   "SECRET_TOOL_INPUT", "SECRET_FILE_CONTENT", "SECRET_FINAL", "call1"):
        assert secret not in encoded


def test_unconfigured_pricing_is_null_and_never_invented(tmp_path, monkeypatch):
    monkeypatch.delenv("TRACE_INPUT_USD_PER_MILLION", raising=False)
    monkeypatch.delenv("TRACE_OUTPUT_USD_PER_MILLION", raising=False)
    path = tmp_path / "trace-example.jsonl"
    write_events(path)
    result = analyze_trace(path, "local-id")
    assert result["pricing_configured"] is False
    assert result["runs"][0]["cost_usd_estimate"] is None


def test_trace_api_rejects_unowned_paths_and_unknown_session(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("MYCODEAGENT_DATA_DIR", str(tmp_path / "state"))
    # No real Agent, LLM or credentials required.
    app = create_app(agent_factory=lambda: SimpleNamespace())
    with TestClient(app) as client:
        assert client.get("/api/sessions/../trace").status_code in (404, 405)
        assert client.get("/api/sessions/invalidid/trace").status_code == 404
        sid = client.post("/api/sessions", json={}).json()["id"]
        assert client.get(f"/api/sessions/{sid}/trace").json()["available"] is False

        dirpath = tmp_path / "traces"
        dirpath.mkdir()
        path = dirpath / "trace-safe-identifier.jsonl"
        write_events(path)
        session = app.state.controller.get_session(sid)
        session._agent = SimpleNamespace(trace_logger=SimpleNamespace(
            trace_dir=dirpath, session_id="safe-identifier", _filepath=path,
        ), close=lambda: None)
        response = client.get(f"/api/sessions/{sid}/trace")
        assert response.status_code == 200
        assert response.json()["trace_id"] == "safe-identifier"
        assert "SECRET_FINAL" not in response.text

        # The server never reads even an otherwise valid Trace filename from
        # an unrelated directory.
        other = tmp_path / "other"
        other.mkdir()
        badpath = other / "trace-safe-identifier.jsonl"
        write_events(badpath)
        session._agent.trace_logger._filepath = badpath
        assert client.get(f"/api/sessions/{sid}/trace").json()["available"] is False


def test_tail_and_malformed_lines_are_bounded(tmp_path):
    path = tmp_path / "trace-big.jsonl"
    with path.open("w", encoding="utf-8") as stream:
        stream.write("a" * (4 * 1024 * 1024 + 20) + "\n")
        stream.write('{"event":"run_start","step":0,"payload":{"run_id":2}}\n')
    result = analyze_trace(path, "big")
    assert result["truncated"] is True
    assert isinstance(result["runs"], list)
