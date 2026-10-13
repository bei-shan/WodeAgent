"""Ensure Web cancellation reaches the real CodeAgent ReAct loop."""
import threading

from agents.codeAgent import CodeAgent


def test_interrupt_stops_before_another_llm_step():
    agent = CodeAgent.__new__(CodeAgent)
    agent._interrupt_requested = threading.Event()
    agent.max_steps = 3
    agent.interrupt()
    assert agent._interrupt_requested.is_set()
    # Should return before any LLM/trace/event dependencies are accessed.
    result = agent._react_loop(pending_input="hello", show_raw=False, trace_logger=None)
    assert result == "已停止生成（用户中断）"
