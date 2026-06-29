"""Step 27 — background task observer + TaskOutput rich info.

Covers:
- BackgroundTaskRunner.register_observer / unregister_observer
- Observer receives progress records during task execution
- Observer exceptions don't break the task
- get_rich_status returns last_step / current_tool / last_event
- get_status returns string (backward compat)

Run:
    python -m pytest tests/test_subagent_streaming_step27.py -v
"""

from __future__ import annotations

import json
import time
from unittest.mock import Mock

import pytest

from core.background_task import BackgroundTaskRunner
from tools.registry import ToolRegistry


# ---------------------------------------------------------------------------
# Observer pattern
# ---------------------------------------------------------------------------


def test_observer_receives_progress_records(tmp_path):
    """Registered observer must be called for each progress entry."""
    events: list[tuple[str, dict]] = []

    def observer(tid: str, rec: dict) -> None:
        events.append((tid, rec))

    runner = BackgroundTaskRunner(tmp_path)
    runner.register_observer(observer)

    def _task(progress_cb=None):
        if progress_cb:
            progress_cb(1, "thought", {"content": "thinking..."})
            progress_cb(2, "action", {"tool": "Read", "input": "x"})
            progress_cb(3, "thought", {"content": "done"})
        return ("result", {"Read": 1})

    runner.launch("task-obs", _task, "test observer")
    # Give the daemon thread a moment to complete.
    for _ in range(50):
        if runner.get_status("task-obs") != "running":
            break
        time.sleep(0.05)
    else:
        pytest.fail("task-obs did not complete within timeout")

    assert len(events) >= 3
    assert events[0][0] == "task-obs"
    assert events[0][1]["type"] == "thought"
    assert events[1][1]["type"] == "action"
    assert events[1][1]["tool"] == "Read"


def test_observer_exception_does_not_break_task(tmp_path):
    """A broken observer must not prevent the task from completing."""
    def broken(_tid: str, _rec: dict) -> None:
        raise RuntimeError("observing failed")

    runner = BackgroundTaskRunner(tmp_path)
    runner.register_observer(broken)

    def _task(progress_cb=None):
        if progress_cb:
            progress_cb(1, "action", {"tool": "Read"})
        return ("ok", {})

    runner.launch("task-crash", _task)
    for _ in range(50):
        if runner.get_status("task-crash") != "running":
            break
        time.sleep(0.05)
    else:
        pytest.fail("task-crash did not complete within timeout")

    # JSONL must still exist and be valid.
    assert runner.get_status("task-crash") == "completed"
    progress = runner.get_progress("task-crash")
    assert any(e.get("tool") == "Read" for e in progress)


def test_unregister_observer_stops_receiving(tmp_path):
    """After unregister, the observer stops being called."""
    events: list[tuple[str, dict]] = []

    def obs(tid: str, rec: dict) -> None:
        events.append((tid, rec))

    runner = BackgroundTaskRunner(tmp_path)
    runner.register_observer(obs)
    runner.unregister_observer(obs)

    def _task(progress_cb=None):
        if progress_cb:
            progress_cb(1, "action", {"tool": "Read"})
        return ("done", {})

    runner.launch("task-unreg", _task)
    for _ in range(50):
        if runner.get_status("task-unreg") != "running":
            break
        time.sleep(0.05)
    else:
        pytest.fail("task-unreg did not complete")

    assert events == []  # unregistered → no calls


# ---------------------------------------------------------------------------
# get_rich_status
# ---------------------------------------------------------------------------


def test_get_rich_status_running(tmp_path):
    """Running task must expose last_step / current_tool via rich status."""
    stop = [False]

    def _task_long(progress_cb=None):
        i = 0
        while not stop[0]:
            i += 1
            if progress_cb:
                progress_cb(i, "action", {"tool": "Read", "input": str(i)})
            time.sleep(0.02)

    runner = BackgroundTaskRunner(tmp_path)
    runner.launch("task-rich", _task_long)

    # Wait for at least 1 progress entry.
    for _ in range(80):
        rich = runner.get_rich_status("task-rich")
        if rich.get("current_tool"):
            break
        time.sleep(0.05)
    else:
        stop[0] = True
        pytest.fail("No progress record appeared")

    rich = runner.get_rich_status("task-rich")
    assert rich["status"] == "running"
    assert rich["current_tool"] == "Read"
    assert rich["last_step"] >= 1
    assert rich["last_event"]["tool"] == "Read"

    stop[0] = True


def test_get_rich_status_not_found(tmp_path):
    assert BackgroundTaskRunner(tmp_path).get_rich_status("nope") == {"status": "not_found"}


def test_get_rich_status_completed(tmp_path):
    runner = BackgroundTaskRunner(tmp_path)

    def _task(progress_cb=None):
        if progress_cb:
            progress_cb(1, "thought", {"content": "x"})
        return ("done", {"Read": 1})

    runner.launch("task-done", _task)
    for _ in range(50):
        if runner.get_status("task-done") != "running":
            break
        time.sleep(0.05)

    rich = runner.get_rich_status("task-done")
    assert rich["status"] == "completed"


# ---------------------------------------------------------------------------
# get_status backward compat (string interface — legacy/discard backward tests should stay)
# ---------------------------------------------------------------------------


def test_get_status_still_returns_string(tmp_path):
    runner = BackgroundTaskRunner(tmp_path)
    assert runner.get_status("no-such-task-id") == "not_found"
