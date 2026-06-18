"""End-to-end tests for CodeAgent — full pipeline with mock LLM.

Run:
    python -m pytest tests/test_e2e_agent.py -v
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest

from core.config import Config
from tools.registry import ToolRegistry
from tools.builtin.read_file import ReadTool
from tools.builtin.write_file import WriteTool
from tools.builtin.edit_file import EditTool
from tools.builtin.todo_write import TodoWriteTool


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class ScriptedMockLLM:
    """Mock LLM that returns scripted responses in order.

    Each response is a dict matching OpenAI ChatCompletion shape:
        {"choices": [{"message": {"content": "...", "tool_calls": [...]}}]}
    """

    def __init__(self, responses: list[dict[str, Any]]):
        self._responses = responses
        self.call_count = 0
        self.calls: list[dict[str, Any]] = []

    def invoke_raw(self, messages, tools=None, tool_choice=None):
        """Record call args and return the next scripted response."""
        self.calls.append({
            "messages": messages,
            "tools": tools,
            "tool_choice": tool_choice,
        })
        if self.call_count >= len(self._responses):
            raise RuntimeError(
                f"Mock LLM exhausted: {self.call_count} calls, "
                f"only {len(self._responses)} responses configured"
            )
        response = self._responses[self.call_count]
        self.call_count += 1
        return response


def make_response(
    content: str = "",
    tool_calls: list[dict[str, Any]] | None = None,
    usage: dict[str, int] | None = None,
) -> dict[str, Any]:
    """Build a mock OpenAI ChatCompletion response dict.

    Args:
        content: Text content from the assistant.
        tool_calls: Optional list of tool call dicts in OpenAI format:
            [{"id": "c1", "type": "function",
              "function": {"name": "Read", "arguments": '{"path":"f.txt"}'}}]
        usage: Optional token usage dict.
    """
    message: dict[str, Any] = {"content": content}
    if tool_calls is not None:
        message["tool_calls"] = tool_calls
    response: dict[str, Any] = {"choices": [{"message": message}]}
    if usage:
        response["usage"] = usage
    return response


def _make_agent(
    tmp_path: Path,
    mock_llm: ScriptedMockLLM,
    extra_tools: list | None = None,
    **kwargs,
):
    """Create a minimal CodeAgent for testing.

    Uses ``tmp_path`` as project_root so tool prompts (loaded from disk)
    are empty — the mock LLM doesn't need them.  Builtin tools are
    registered explicitly.
    """
    from agents.codeAgent import CodeAgent

    registry = ToolRegistry()
    for tool_cls in [ReadTool, WriteTool, EditTool, TodoWriteTool]:
        registry.register_tool(tool_cls(project_root=tmp_path))
    if extra_tools:
        for tool in extra_tools:
            registry.register_tool(tool)

    config = Config(
        context_window=128000,
        compression_threshold=0.8,
        min_retain_rounds=2,
        debug=False,
        show_react_steps=False,
        show_progress=False,
    )
    config.enable_agent_teams = False

    return CodeAgent(
        name="e2e-test",
        llm=mock_llm,
        tool_registry=registry,
        project_root=str(tmp_path),
        config=config,
        **kwargs,
    )


# ---------------------------------------------------------------------------
# Test 1: Direct answer — no tool calls
# ---------------------------------------------------------------------------


def test_e2e_direct_answer(tmp_path: Path):
    """Agent gives a direct text answer without using any tools."""
    (tmp_path / "README.md").write_text("# Hello\n", encoding="utf-8")

    mock_llm = ScriptedMockLLM([
        make_response(content="项目包含一个 README.md 文件。"),
    ])

    agent = _make_agent(tmp_path, mock_llm)
    result = agent.run("这个项目是什么？")

    assert "README" in result
    assert mock_llm.call_count == 1
    assert agent.history_manager.get_rounds_count() == 1


# ---------------------------------------------------------------------------
# Test 2: Read file — one tool call
# ---------------------------------------------------------------------------


def test_e2e_read_file(tmp_path: Path):
    """Agent reads a file and reports its content."""
    (tmp_path / "config.json").write_text('{"version": "1.0"}\n', encoding="utf-8")

    mock_llm = ScriptedMockLLM([
        make_response(
            tool_calls=[{
                "id": "call_1",
                "type": "function",
                "function": {
                    "name": "Read",
                    "arguments": json.dumps({"path": "config.json"}),
                },
            }],
        ),
        make_response(content="配置文件版本是 1.0。"),
    ])

    agent = _make_agent(tmp_path, mock_llm)
    result = agent.run("读取 config.json 告诉我版本")

    assert "1.0" in result
    assert mock_llm.call_count == 2
    # Verify Read was actually called with the right tool
    assert mock_llm.calls[0]["tools"] is not None  # tools schema was passed


# ---------------------------------------------------------------------------
# Test 3: Read → Edit — optimistic lock path
# ---------------------------------------------------------------------------


def test_e2e_read_then_edit(tmp_path: Path):
    """Agent reads a file then edits it — validates the full pipeline."""
    (tmp_path / "todo.txt").write_text("buy milk\n", encoding="utf-8")

    mock_llm = ScriptedMockLLM([
        # Step 1: Read the file
        make_response(
            tool_calls=[{
                "id": "call_1",
                "type": "function",
                "function": {
                    "name": "Read",
                    "arguments": json.dumps({"path": "todo.txt"}),
                },
            }],
        ),
        # Step 2: Edit the file (optimistic lock auto-injected by registry)
        make_response(
            tool_calls=[{
                "id": "call_2",
                "type": "function",
                "function": {
                    "name": "Edit",
                    "arguments": json.dumps({
                        "path": "todo.txt",
                        "old_string": "buy milk",
                        "new_string": "buy milk\nbuy eggs",
                    }),
                },
            }],
        ),
        # Step 3: Done
        make_response(content="已在 todo.txt 中添加 buy eggs。"),
    ])

    agent = _make_agent(tmp_path, mock_llm)
    result = agent.run("在 todo.txt 里加一项 buy eggs")

    assert "eggs" in result
    content = (tmp_path / "todo.txt").read_text("utf-8")
    assert "buy eggs" in content
    assert mock_llm.call_count == 3


# ---------------------------------------------------------------------------
# Test 4: Read → Write (new file)
# ---------------------------------------------------------------------------


def test_e2e_write_new_file(tmp_path: Path):
    """Agent creates a new file via Write tool."""
    mock_llm = ScriptedMockLLM([
        make_response(
            tool_calls=[{
                "id": "call_1",
                "type": "function",
                "function": {
                    "name": "Write",
                    "arguments": json.dumps({
                        "path": "output.txt",
                        "content": "generated content\n",
                    }),
                },
            }],
        ),
        make_response(content="已创建 output.txt。"),
    ])

    agent = _make_agent(tmp_path, mock_llm)
    result = agent.run("创建 output.txt 写入 generated content")

    assert "output.txt" in result
    assert (tmp_path / "output.txt").exists()
    assert (tmp_path / "output.txt").read_text("utf-8") == "generated content\n"
    assert mock_llm.call_count == 2


# ---------------------------------------------------------------------------
# Test 5: Multi-step with TodoWrite
# ---------------------------------------------------------------------------


def test_e2e_multi_step_with_todos(tmp_path: Path):
    """Agent plans with TodoWrite, then executes."""
    (tmp_path / "src").mkdir(parents=True)
    (tmp_path / "src" / "main.py").write_text("print('hello')\n", encoding="utf-8")

    mock_llm = ScriptedMockLLM([
        # Step 1: Plan with TodoWrite
        make_response(
            tool_calls=[{
                "id": "call_1",
                "type": "function",
                "function": {
                    "name": "TodoWrite",
                    "arguments": json.dumps({
                        "todos": [
                            {"content": "Read main.py", "status": "in_progress"},
                            {"content": "Report findings", "status": "pending"},
                        ],
                    }),
                },
            }],
        ),
        # Step 2: Read main.py
        make_response(
            tool_calls=[{
                "id": "call_2",
                "type": "function",
                "function": {
                    "name": "Read",
                    "arguments": json.dumps({"path": "src/main.py"}),
                },
            }],
        ),
        # Step 3: Done
        make_response(content="main.py 打印 hello。"),
    ])

    agent = _make_agent(tmp_path, mock_llm)
    result = agent.run("分析 src/main.py")

    assert "hello" in result.lower()
    assert mock_llm.call_count == 3


# ---------------------------------------------------------------------------
# Test 6: Empty response retry
# ---------------------------------------------------------------------------


def test_e2e_empty_response_retry(tmp_path: Path):
    """When LLM returns empty response, agent retries once with a hint."""
    mock_llm = ScriptedMockLLM([
        # First response: empty (should trigger retry)
        make_response(content=""),
        # Second response: works (after retry hint)
        make_response(content="已重试成功。"),
    ])

    agent = _make_agent(tmp_path, mock_llm)
    result = agent.run("随便问点什么")

    assert "重试" in result or "成功" in result
    # Both initial and retry calls happened
    assert mock_llm.call_count >= 2


# ---------------------------------------------------------------------------
# Test 7: History compression trigger (many steps)
# ---------------------------------------------------------------------------


def test_e2e_many_steps_triggers_compression(tmp_path: Path):
    """Long conversation triggers history compression."""
    # Create files for many Read calls
    for i in range(20):
        (tmp_path / f"file_{i:02d}.txt").write_text(f"content_{i}\n", encoding="utf-8")

    # Build script: 15 Read calls + final answer
    responses = []
    for i in range(15):
        responses.append(make_response(
            tool_calls=[{
                "id": f"call_{i}",
                "type": "function",
                "function": {
                    "name": "Read",
                    "arguments": json.dumps({"path": f"file_{i:02d}.txt"}),
                },
            }],
            usage={"prompt_tokens": 500, "completion_tokens": 50, "total_tokens": 550},
        ))
    responses.append(make_response(content="已读取全部文件。"))

    mock_llm = ScriptedMockLLM(responses)

    # Small context window to force early compression
    config = Config(
        context_window=4000,
        compression_threshold=0.5,  # trigger at ~2000 tokens
        min_retain_rounds=2,
        debug=False,
        show_react_steps=False,
        show_progress=False,
    )
    config.enable_agent_teams = False

    registry = ToolRegistry()
    for tool_cls in [ReadTool, TodoWriteTool]:
        registry.register_tool(tool_cls(project_root=tmp_path))

    from agents.codeAgent import CodeAgent

    agent = CodeAgent(
        name="e2e-test",
        llm=mock_llm,
        tool_registry=registry,
        project_root=str(tmp_path),
        config=config,
    )

    result = agent.run("读取所有 file_*.txt 文件")

    assert "读取" in result
    assert mock_llm.call_count == 16

    # Verify compression happened (fewer rounds than steps due to compaction)
    rounds = agent.history_manager.get_rounds_count()
    # With 15 tool-call rounds, compression should have reduced round count
    assert rounds < 15, f"Expected compression, got {rounds} rounds (should be < 15)"


# ---------------------------------------------------------------------------
# Test 8: History message format correctness
# ---------------------------------------------------------------------------


def test_e2e_history_messages_format(tmp_path: Path):
    """Verify that history messages are in valid OpenAI format after a run."""
    (tmp_path / "data.txt").write_text("hello\n", encoding="utf-8")

    mock_llm = ScriptedMockLLM([
        make_response(
            tool_calls=[{
                "id": "call_1",
                "type": "function",
                "function": {
                    "name": "Read",
                    "arguments": json.dumps({"path": "data.txt"}),
                },
            }],
        ),
        make_response(content="文件内容是 hello。"),
    ])

    agent = _make_agent(tmp_path, mock_llm)
    agent.run("读 data.txt")

    # Get history as OpenAI-format messages
    messages = agent.history_manager.to_messages()

    # Should have: user, assistant (tool_call), tool, assistant (final)
    roles = [m["role"] for m in messages]
    assert "user" in roles
    assert "assistant" in roles
    assert "tool" in roles

    # Assistant messages with tool_calls should have the tool_calls field
    assistant_msgs = [m for m in messages if m["role"] == "assistant"]
    tool_call_msg = assistant_msgs[0]
    assert "tool_calls" in tool_call_msg

    # Tool messages should have tool_call_id
    tool_msgs = [m for m in messages if m["role"] == "tool"]
    assert "tool_call_id" in tool_msgs[0]
