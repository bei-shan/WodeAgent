"""Configuration must belong to a session, not the last CodeAgent initialized."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from types import SimpleNamespace

from core.agent import Agent
from core.config import Config
from core.runtime.config_scope import active_config, scoped_config
from core.context_engine.observation_truncator import _get_max_lines, _get_max_bytes
from tools.builtin.task import TaskTool
from tools.registry import ToolRegistry


class SmallAgent(Agent):
    def run(self, input_text: str, **kwargs) -> str:
        return input_text


def test_base_agent_clones_mutable_config_before_session_changes():
    supplied = Config(enable_agent_teams=False, circuit_failure_threshold=5)
    a = SmallAgent("a", SimpleNamespace(), config=supplied)
    b = SmallAgent("b", SimpleNamespace(), config=supplied)
    assert a.config is not b.config and a.config is not supplied
    a.config.enable_agent_teams = True
    a.config.circuit_failure_threshold = 2
    assert b.config.enable_agent_teams is False
    assert b.config.circuit_failure_threshold == 5
    assert supplied.enable_agent_teams is False


def test_scoped_config_is_concurrent_and_restored_after_failure():
    barrier = Barrier(2)
    a = Config(tool_output_max_lines=20, tool_output_max_bytes=1234)
    b = Config(tool_output_max_lines=99, tool_output_max_bytes=9876)

    def worker(cfg):
        with scoped_config(cfg):
            barrier.wait(timeout=5)
            return active_config(), _get_max_lines(), _get_max_bytes()

    with ThreadPoolExecutor(max_workers=2) as pool:
        f1 = pool.submit(worker, a)
        f2 = pool.submit(worker, b)
        assert f1.result() == (a, 20, 1234)
        assert f2.result() == (b, 99, 9876)
    assert active_config() is None

    try:
        with scoped_config(a):
            raise RuntimeError("broken turn")
    except RuntimeError:
        pass
    assert active_config() is None


def test_nested_scopes_restore_previous_agent_config():
    a, b = Config(max_steps=3), Config(max_steps=77)
    with scoped_config(a):
        assert active_config() is a
        with scoped_config(b):
            assert active_config() is b
        assert active_config() is a
    assert active_config() is None


def test_registry_owns_circuit_breaker_config():
    a = Config(circuit_failure_threshold=1, circuit_recovery_timeout=1)
    b = Config(circuit_failure_threshold=5, circuit_recovery_timeout=500)
    first = ToolRegistry(config=a)
    second = ToolRegistry(config=b)
    first._circuit_breaker.record_failure("Read", "boom")
    second._circuit_breaker.record_failure("Read", "boom")
    assert first._circuit_breaker.is_available("Read") is False
    assert second._circuit_breaker.is_available("Read") is True
    assert second._circuit_breaker.recovery_timeout == 500


def test_task_receives_own_agent_config(tmp_path):
    a, b = Config(subagent_max_steps=3), Config(subagent_max_steps=21)
    registry = ToolRegistry(config=a)
    task_a = TaskTool(
        project_root=tmp_path, main_llm=SimpleNamespace(),
        tool_registry=registry, config=a,
    )
    task_b = TaskTool(
        project_root=tmp_path, main_llm=SimpleNamespace(),
        tool_registry=ToolRegistry(config=b), config=b,
    )
    assert task_a._subagent_max_steps == 3
    assert task_b._subagent_max_steps == 21
    assert task_a._config is a and task_b._config is b


def test_codeagent_run_binds_config_without_constructing_full_engine():
    # The outer public entrypoint binds context even when the inner loop is mocked.
    from agents.codeAgent import CodeAgent
    agent = CodeAgent.__new__(CodeAgent)
    config = Config(tool_output_max_lines=37)
    agent.config = config

    def inner(_message: str, **kwargs):
        assert active_config() is config
        return _get_max_lines()

    agent._run_scoped = inner
    assert agent.run("hello") == 37
    assert active_config() is None
