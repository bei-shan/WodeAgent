"""A background runner must never replay a callable after it raises."""

from __future__ import annotations

import time

import pytest

from core.background_task import BackgroundTaskRunner


def _await_result(background: BackgroundTaskRunner, task_id: str) -> dict:
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        result = background.get_result(task_id)
        if result is not None:
            return result
        time.sleep(0.01)
    pytest.fail(f"background task {task_id} did not persist a result")


@pytest.mark.parametrize("error_type", [TypeError, ValueError])
def test_runner_error_after_effect_is_persisted_without_replay(tmp_path, error_type):
    background = BackgroundTaskRunner(tmp_path)
    effects = []

    def work(progress_callback=None):
        effects.append("tool effect")
        if len(effects) == 1:
            raise error_type("provider failed after tool effect")
        return "false completion", {}

    background.launch("failed-once", work)
    result = _await_result(background, "failed-once")

    assert effects == ["tool effect"]
    assert result["status"] == "failed"
    assert "provider failed after tool effect" in result["result"]
    assert result["result"] != "false completion"


def test_runner_with_callback_completes_once(tmp_path):
    background = BackgroundTaskRunner(tmp_path)
    callbacks = []

    def work(progress_callback):
        callbacks.append(progress_callback)
        progress_callback(1, "tool", {"tool": "Read"})
        return "callback result", {"Read": 1}

    background.launch("callback", work)
    result = _await_result(background, "callback")

    assert len(callbacks) == 1
    assert result["status"] == "completed"
    assert result["result"] == "callback result"
    assert result["tool_usage"] == {"Read": 1}
    assert background.get_progress("callback") == [
        {"step": 1, "type": "tool", "tool": "Read"}
    ]


def test_noarg_runner_completes_once(tmp_path):
    background = BackgroundTaskRunner(tmp_path)
    calls = []

    def work():
        calls.append(True)
        return "noarg result", {}

    background.launch("noarg", work)
    result = _await_result(background, "noarg")

    assert calls == [True]
    assert result["status"] == "completed"
    assert result["result"] == "noarg result"


@pytest.mark.parametrize("error_type", [TypeError, ValueError])
def test_uninspectable_noarg_runner_completes_once(tmp_path, error_type):
    background = BackgroundTaskRunner(tmp_path)

    class UninspectableRunner:
        calls = 0

        @property
        def __signature__(self):
            raise error_type("signature unavailable")

        def __call__(self):
            self.calls += 1
            return "fallback result", {}

    work = UninspectableRunner()
    background.launch("uninspectable", work)
    result = _await_result(background, "uninspectable")

    assert work.calls == 1
    assert result["status"] == "completed"
    assert result["result"] == "fallback result"
