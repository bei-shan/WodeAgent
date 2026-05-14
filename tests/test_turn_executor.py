import json
from unittest.mock import Mock

from core.team_engine.turn_executor import TurnExecutor
from tools.registry import ToolRegistry


def _raw(content="", tool_calls=None):
    message = {"content": content}
    if tool_calls is not None:
        message["tool_calls"] = tool_calls
    return {"choices": [{"message": message}]}


def test_turn_executor_returns_final_answer_without_tool_calls(tmp_path):
    llm = Mock()
    llm.invoke_raw.return_value = _raw(content="Final Answer: done")
    executor = TurnExecutor(
        llm=llm,
        tool_registry=ToolRegistry(),
        project_root=tmp_path,
        denied_tools={"Task"},
    )
    messages = [{"role": "system", "content": "x"}, {"role": "user", "content": "y"}]

    result = executor.execute_turn(messages, tool_usage={})
    assert result["done"] is True
    assert "done" in result["final_result"]


def test_turn_executor_blocks_denied_task_tool(tmp_path):
    llm = Mock()
    llm.invoke_raw.return_value = _raw(
        tool_calls=[{
            "id": "call_1",
            "type": "function",
            "function": {"name": "Task", "arguments": json.dumps({"description": "nested"})},
        }]
    )
    executor = TurnExecutor(
        llm=llm,
        tool_registry=ToolRegistry(),
        project_root=tmp_path,
        denied_tools={"Task"},
    )
    messages = [{"role": "system", "content": "x"}, {"role": "user", "content": "y"}]

    result = executor.execute_turn(messages, tool_usage={})
    assert result["done"] is False
    assert any(m.get("role") == "tool" for m in result["messages"])
    tool_msg = [m for m in result["messages"] if m.get("role") == "tool"][-1]
    assert "not allowed" in tool_msg["content"]


def _raw_with_reasoning(content="", reasoning="thinking step", tool_calls=None):
    """Build a raw response with reasoning_content in the message."""
    message = {"content": content, "reasoning_content": reasoning}
    if tool_calls is not None:
        message["tool_calls"] = tool_calls
    return {"choices": [{"message": message}]}


def test_turn_executor_preserves_reasoning_content_on_final(tmp_path):
    """reasoning_content should be stored in assistant message when no tool calls."""
    llm = Mock()
    llm.invoke_raw.return_value = _raw_with_reasoning(
        content="Final answer",
        reasoning="Let me think step by step...",
    )
    executor = TurnExecutor(
        llm=llm,
        tool_registry=ToolRegistry(),
        project_root=tmp_path,
        denied_tools=set(),
    )
    messages = [{"role": "system", "content": "x"}, {"role": "user", "content": "y"}]

    result = executor.execute_turn(messages, tool_usage={})
    assert result["done"] is True

    assistant_msgs = [m for m in result["messages"] if m.get("role") == "assistant"]
    assert len(assistant_msgs) >= 1
    last_assistant = assistant_msgs[-1]
    assert last_assistant.get("reasoning_content") == "Let me think step by step..."


def test_turn_executor_preserves_reasoning_content_with_tool_calls(tmp_path):
    """reasoning_content should be stored in assistant message even when tool calls exist."""
    llm = Mock()
    llm.invoke_raw.return_value = _raw_with_reasoning(
        content="",
        reasoning="I need to read the file first",
        tool_calls=[{
            "id": "call_1",
            "type": "function",
            "function": {"name": "Read", "arguments": json.dumps({"path": "test.py"})},
        }],
    )

    registry = ToolRegistry()
    mock_tool = Mock()
    mock_tool.name = "Read"
    mock_tool.description = "Read a file"
    mock_tool.get_parameters.return_value = []
    mock_tool.run.return_value = json.dumps({"status": "success", "data": {"content": "x"}, "text": "ok"})
    registry.register_tool(mock_tool)

    executor = TurnExecutor(
        llm=llm,
        tool_registry=registry,
        project_root=tmp_path,
        denied_tools=set(),
    )
    messages = [{"role": "system", "content": "x"}, {"role": "user", "content": "y"}]

    result = executor.execute_turn(messages, tool_usage={})
    assert result["done"] is False

    assistant_msgs = [m for m in result["messages"] if m.get("role") == "assistant" and m.get("tool_calls")]
    assert len(assistant_msgs) >= 1
    last_assistant = assistant_msgs[-1]
    assert last_assistant.get("reasoning_content") == "I need to read the file first"


def test_turn_executor_no_reasoning_field_when_absent(tmp_path):
    """When LLM returns no reasoning_content, assistant msg should not have the field."""
    llm = Mock()
    llm.invoke_raw.return_value = _raw(content="Simple answer", tool_calls=None)
    executor = TurnExecutor(
        llm=llm,
        tool_registry=ToolRegistry(),
        project_root=tmp_path,
        denied_tools=set(),
    )
    messages = [{"role": "system", "content": "x"}, {"role": "user", "content": "y"}]

    result = executor.execute_turn(messages, tool_usage={})
    assert result["done"] is True

    assistant_msgs = [m for m in result["messages"] if m.get("role") == "assistant"]
    last_assistant = assistant_msgs[-1]
    assert "reasoning_content" not in last_assistant


def test_turn_executor_reasoning_in_dict_response(tmp_path):
    """reasoning_content should be extracted from dict-format responses too."""
    raw = {"choices": [{"message": {"content": "answer", "reasoning": "deep thinking"}}]}
    llm = Mock()
    llm.invoke_raw.return_value = raw
    executor = TurnExecutor(
        llm=llm,
        tool_registry=ToolRegistry(),
        project_root=tmp_path,
        denied_tools=set(),
    )
    messages = [{"role": "system", "content": "x"}, {"role": "user", "content": "y"}]

    result = executor.execute_turn(messages, tool_usage={})
    assistant_msgs = [m for m in result["messages"] if m.get("role") == "assistant"]
    assert assistant_msgs[-1].get("reasoning_content") == "deep thinking"


def test_turn_executor_reasoning_multi_turn(tmp_path):
    """Simulate two turns: reasoning from turn 1 should be in messages for turn 2."""
    llm = Mock()
    llm.invoke_raw.side_effect = [
        _raw_with_reasoning(
            content="",
            reasoning="Step 1 thinking",
            tool_calls=[{
                "id": "call_1",
                "type": "function",
                "function": {"name": "Read", "arguments": json.dumps({"path": "a.py"})},
            }],
        ),
        _raw_with_reasoning(
            content="Done",
            reasoning="Step 2 thinking",
        ),
    ]

    registry = ToolRegistry()
    mock_tool = Mock()
    mock_tool.name = "Read"
    mock_tool.description = "Read"
    mock_tool.get_parameters.return_value = []
    mock_tool.run.return_value = json.dumps({"status": "success", "data": {}, "text": "ok"})
    registry.register_tool(mock_tool)

    executor = TurnExecutor(
        llm=llm,
        tool_registry=registry,
        project_root=tmp_path,
        denied_tools=set(),
    )
    messages = [{"role": "system", "content": "sys"}, {"role": "user", "content": "do it"}]

    # Turn 1
    result1 = executor.execute_turn(messages, tool_usage={})
    assert result1["done"] is False

    # Verify turn 1 assistant message has reasoning
    assistant1 = [m for m in result1["messages"] if m.get("role") == "assistant"][-1]
    assert assistant1["reasoning_content"] == "Step 1 thinking"

    # Turn 2: pass the messages from turn 1
    result2 = executor.execute_turn(result1["messages"], tool_usage={})
    assert result2["done"] is True

    # Verify turn 2 assistant message also has reasoning
    assistant2 = [m for m in result2["messages"] if m.get("role") == "assistant"][-1]
    assert assistant2["reasoning_content"] == "Step 2 thinking"

    # Verify the messages passed to turn 2 LLM call contained turn 1's reasoning
    second_call_args = llm.invoke_raw.call_args_list[1]
    sent_messages = second_call_args[0][0]
    sent_assistant = [m for m in sent_messages if m.get("role") == "assistant"][-1]
    assert sent_assistant.get("reasoning_content") == "Step 1 thinking"
