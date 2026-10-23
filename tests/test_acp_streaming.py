"""ACP streaming: tool_call and tool_call_update detail from agent events."""
from __future__ import annotations

import io
import json

from core.events import AgentEvent, EventType
from ridgecode.acp import AcpServer


class FakeAgent:
    def __init__(self):
        self.event_sink = None

    def run(self, text: str) -> str:
        return text


def _server():
    out = io.StringIO()
    return AcpServer(lambda cwd: FakeAgent(), stdout=out), out


def _updates(out: io.StringIO) -> list[dict]:
    return [
        json.loads(line)["params"]["update"]
        for line in out.getvalue().splitlines()
        if line.strip() and "session/update" in line
    ]


def test_tool_started_maps_kind_input_and_id():
    server, out = _server()
    server._forward_event("sid", AgentEvent(EventType.TOOL_STARTED, {
        "tool": "Edit", "input": {"path": "a.py", "old_string": "x"}, "tool_call_id": "c1",
    }))
    update = _updates(out)[0]
    assert update["sessionUpdate"] == "tool_call"
    assert update["toolCallId"] == "c1"
    assert update["title"] == "Edit"
    assert update["kind"] == "edit"
    assert update["status"] == "in_progress"
    assert update["rawInput"]["path"] == "a.py"


def test_tool_kinds_cover_read_search_execute_and_unknown():
    server, out = _server()
    for name in ("Read", "Grep", "Bash", "WebFetch", "Workflow", "Mystery"):
        server._forward_event("sid", AgentEvent(EventType.TOOL_STARTED, {
            "tool": name, "input": {}, "tool_call_id": name,
        }))
    kinds = [update["kind"] for update in _updates(out)]
    assert kinds == ["read", "search", "execute", "fetch", "other", "other"]


def test_tool_completed_reports_status_and_readable_output():
    server, out = _server()
    server._forward_event("sid", AgentEvent(EventType.TOOL_COMPLETED, {
        "tool": "Read",
        "tool_call_id": "c2",
        "status": "success",
        "output": json.dumps({"status": "success", "text": "file contents", "data": {}}),
    }))
    update = _updates(out)[0]
    assert update["sessionUpdate"] == "tool_call_update"
    assert update["toolCallId"] == "c2"
    assert update["status"] == "completed"
    assert update["content"] == [{"type": "text", "text": "file contents"}]


def test_tool_completed_failure_maps_to_failed():
    server, out = _server()
    server._forward_event("sid", AgentEvent(EventType.TOOL_COMPLETED, {
        "tool": "Bash", "tool_call_id": "c3", "status": "error",
        "output": json.dumps({"status": "error", "error": {"message": "boom"}}),
    }))
    update = _updates(out)[0]
    assert update["status"] == "failed"


def test_tool_output_is_truncated_and_non_json_is_kept_raw():
    server, out = _server()
    server._forward_event("sid", AgentEvent(EventType.TOOL_COMPLETED, {
        "tool": "Bash", "tool_call_id": "c4", "status": "success",
        "output": json.dumps({"status": "success", "text": "x" * 5000}),
    }))
    server._forward_event("sid", AgentEvent(EventType.TOOL_COMPLETED, {
        "tool": "Bash", "tool_call_id": "c5", "status": "success", "output": "plain text",
    }))
    first, second = _updates(out)
    assert len(first["content"][0]["text"]) == 2000
    assert second["content"] == [{"type": "text", "text": "plain text"}]


def test_missing_ids_fall_back_to_the_tool_name():
    server, out = _server()
    server._forward_event("sid", AgentEvent(EventType.TOOL_STARTED, {
        "tool": "Read", "input": {},
    }))
    assert _updates(out)[0]["toolCallId"] == "Read"
