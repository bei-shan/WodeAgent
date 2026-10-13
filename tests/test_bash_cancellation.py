"""Cancellation reaches a running Bash process, not just the next turn."""
from __future__ import annotations

import json
import logging
import os
import sys
import threading
import time
from pathlib import Path

import pytest

from agents.codeAgent import CodeAgent
from core.runtime.cancellation import current_cancel_event, scoped_tool_cancellation
from core.runtime.process_execution import run_process
from tools.builtin.bash import BashTool
from tools.registry import ToolRegistry


def test_scoped_cancellation_is_isolated_and_restored():
    event = threading.Event()
    assert current_cancel_event() is None
    with scoped_tool_cancellation(event):
        assert current_cancel_event() is event
    assert current_cancel_event() is None


def test_already_cancelled_process_never_starts(tmp_path):
    event = threading.Event()
    event.set()
    result = run_process(
        ["this-program-does-not-exist"], shell=False, cwd=str(tmp_path),
        env=os.environ.copy(), timeout=5, cancel_event=event,
    )
    assert result.interrupted is True
    assert result.returncode is None


def test_agent_interrupt_stops_current_bash_and_preserves_error_protocol(tmp_path):
    script = tmp_path / "sleeper.py"
    started = tmp_path / "started.txt"
    finished = tmp_path / "finished.txt"
    script.write_text(
        "from pathlib import Path\nimport time\n"
        "Path('started.txt').write_text('ready')\n"
        "time.sleep(5)\nPath('finished.txt').write_text('finished')\n"
    )
    agent = CodeAgent.__new__(CodeAgent)
    agent.tool_registry = ToolRegistry()
    agent.tool_registry.register_tool(BashTool(project_root=tmp_path))
    agent.logger = logging.getLogger("cancel-test")
    agent._features = []
    agent._hook_system_messages = []
    agent._interrupt_requested = threading.Event()
    observed = []

    def worker():
        observed.append(agent._execute_tool("Bash", {"command": "python sleeper.py"}))

    thread = threading.Thread(target=worker)
    thread.start()
    until = time.monotonic() + 3
    while not started.exists() and time.monotonic() < until and thread.is_alive():
        time.sleep(0.02)
    assert started.exists(), "process did not start"
    agent.interrupt()
    thread.join(timeout=3)
    assert not thread.is_alive(), "interrupted tool remained blocked"
    assert json.loads(observed[0])["error"]["code"] == "INTERRUPTED"
    assert not finished.exists()


def test_timeout_output_behavior_is_preserved(tmp_path):
    tool = BashTool(project_root=tmp_path)
    result = json.loads(tool.run({
        "command": "python -u -c \"import time; print('start'); time.sleep(3)\"",
        "timeout_ms": 300,
    }))
    assert result["status"] == "partial"
    assert "start" in result["data"]["stdout"]


@pytest.mark.skipif(os.name != "posix", reason="POSIX process group semantics")
def test_cancel_kills_shell_descendants(tmp_path):
    # The shell spawns a grandchild whose only purpose is writing a marker.
    # If cancellation killed only /bin/sh the marker would appear later.
    script = tmp_path / "spawn_child.py"
    child_script = tmp_path / "child.py"
    marker = tmp_path / "survived.txt"
    child_script.write_text(
        "import time\nfrom pathlib import Path\n"
        "time.sleep(1.1)\nPath('survived.txt').write_text('leak')\n"
    )
    script.write_text(
        "import subprocess,sys,time\n"
        "subprocess.Popen([sys.executable,'child.py'])\n"
        "from pathlib import Path\n"
        "Path('running.txt').write_text('go')\n"
        "time.sleep(8)\n"
    )
    ready = tmp_path / "running.txt"
    cancelled = threading.Event()
    outputs = []
    thread = threading.Thread(target=lambda: outputs.append(run_process(
        "python spawn_child.py", shell=True, cwd=str(tmp_path),
        env=os.environ.copy(), timeout=6, cancel_event=cancelled,
    )))
    thread.start()
    until = time.monotonic() + 3
    while not ready.exists() and time.monotonic() < until and thread.is_alive():
        time.sleep(0.02)
    assert ready.exists(), "shell child never started"
    cancelled.set()
    thread.join(timeout=3)
    assert not thread.is_alive()
    assert outputs[0].interrupted is True
    time.sleep(1.3)
    assert not marker.exists(), "grandchild survived process-group cancellation"
