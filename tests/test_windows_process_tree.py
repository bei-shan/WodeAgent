"""Actual Windows Job Object acceptance: descendants must not escape a turn."""
from __future__ import annotations

import os
import sys
import threading
import time

import pytest

from core.runtime.process_execution import run_process


pytestmark = pytest.mark.skipif(os.name != "nt", reason="requires Windows Job Objects")


def _scripts(tmp_path, *, parent_delay=10):
    child = tmp_path / "grandchild.py"
    child.write_text(
        "from pathlib import Path\nimport time\n"
        "time.sleep(2.0)\n"
        "Path('escaped.txt').write_text('not-contained')\n",
        encoding="utf-8",
    )
    parent = tmp_path / "parent.py"
    parent.write_text(
        "import subprocess, sys, time\nfrom pathlib import Path\n"
        "subprocess.Popen([sys.executable, 'grandchild.py'])\n"
        "Path('running.txt').write_text('ready')\n"
        f"time.sleep({parent_delay})\n",
        encoding="utf-8",
    )
    return parent, tmp_path / "running.txt", tmp_path / "escaped.txt"


def _launch(parent, root, *, timeout, event=None):
    return run_process(
        [sys.executable, str(parent)], shell=False,
        cwd=str(root), env=os.environ.copy(),
        timeout=timeout, cancel_event=event, poll_interval=0.05,
    )


def test_windows_cancellation_kills_grandchildren(tmp_path):
    parent, ready, escaped = _scripts(tmp_path)
    cancel = threading.Event()
    outcomes = []
    worker = threading.Thread(target=lambda: outcomes.append(
        _launch(parent, tmp_path, timeout=15, event=cancel)
    ))
    worker.start()
    until = time.monotonic() + 8
    while not ready.exists() and time.monotonic() < until and worker.is_alive():
        time.sleep(0.04)
    try:
        assert ready.exists(), "parent did not start inside its Job Object"
    finally:
        cancel.set()
        worker.join(timeout=6)
    assert not worker.is_alive()
    assert outcomes and outcomes[0].interrupted is True
    time.sleep(2.2)
    assert not escaped.exists(), "grandchild survived cancellation"


def test_windows_timeout_kills_grandchildren(tmp_path):
    parent, _ready, escaped = _scripts(tmp_path)
    result = _launch(parent, tmp_path, timeout=0.8)
    assert result.timed_out is True
    time.sleep(2.2)
    assert not escaped.exists(), "grandchild survived timeout"


def test_windows_successful_parent_does_not_leak_orphans(tmp_path):
    parent, ready, escaped = _scripts(tmp_path, parent_delay=0.7)
    result = _launch(parent, tmp_path, timeout=8)
    assert ready.exists()
    assert result.returncode == 0
    time.sleep(2.2)
    assert not escaped.exists(), "successful shell left a detached child alive"


def test_windows_missing_job_object_fails_before_command(tmp_path, monkeypatch):
    import core.runtime.windows_process_job as jobs

    def denied():
        raise OSError("job object unavailable")

    monkeypatch.setattr(jobs, "WindowsProcessJob", denied)
    with pytest.raises(OSError, match="job object unavailable"):
        run_process(
            [sys.executable, "-c", "open('ran.txt', 'w').write('bad')"],
            shell=False, cwd=str(tmp_path), env=os.environ.copy(), timeout=3,
        )
    assert not (tmp_path / "ran.txt").exists()
