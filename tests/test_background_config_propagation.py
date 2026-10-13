"""Session Config must follow background and team worker execution threads."""
from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from core.background_task import BackgroundTaskRunner
from core.config import Config
from core.runtime.config_scope import active_config, scoped_config
from core.team_engine.execution import ExecutionService


def test_background_task_captures_launching_session_config(tmp_path):
    runner = BackgroundTaskRunner(project_root=Path(tmp_path))
    completed_a, completed_b = threading.Event(), threading.Event()
    config_a = Config(tool_output_max_lines=13)
    config_b = Config(tool_output_max_lines=19)
    results = {}

    def launch(name, config, done):
        def task(progress_cb):
            results[name] = active_config()
            done.set()
            return "ok", {}
        with scoped_config(config):
            runner.launch("task_" + name, task, "test")
    launch("a", config_a, completed_a)
    launch("b", config_b, completed_b)
    assert completed_a.wait(3) and completed_b.wait(3)
    assert results == {"a": config_a, "b": config_b}
    assert active_config() is None


def test_team_work_executor_runs_with_own_config_on_unrelated_workers():
    a = Config(tool_output_max_lines=111)
    b = Config(tool_output_max_lines=222)

    def make(config):
        return ExecutionService(
            project_root=".", llm=None, tool_registry=None,
            work_executor=lambda work: {"limit": active_config().tool_output_max_lines},
            read_team_fn=lambda _: {}, config=config,
        )
    workers = [make(a), make(b)]
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(w.execute_work_item, "demo", "worker", {}) for w in workers]
        assert [f.result()["limit"] for f in futures] == [111, 222]
    assert active_config() is None


def test_bound_context_preserves_source_and_restores_worker_thread():
    from core.runtime.config_scope import bind_current_context
    config = Config(tool_output_max_bytes=345)
    with scoped_config(config):
        bound = bind_current_context(lambda: active_config())
    with ThreadPoolExecutor(max_workers=1) as pool:
        assert pool.submit(bound).result() is config
        assert pool.submit(active_config).result() is None
    assert active_config() is None
