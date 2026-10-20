"""Bash output must be bounded before serialization, not only in model text."""
from __future__ import annotations

import json
import os
import sys
import threading
import time

import pytest

from core.runtime.process_execution import run_process
from tools.builtin.bash import BashTool


def _python_script(code):
    return [sys.executable, "-u", "-c", code]


def test_runner_drains_many_megabytes_but_retains_only_bounded_prefix(tmp_path):
    result = run_process(
        _python_script("import os; os.write(1,b'A'*2000000); os.write(2,b'B'*2000000)"),
        shell=False, cwd=str(tmp_path), env=os.environ.copy(), timeout=10,
        max_output_bytes=4096,
    )
    assert result.returncode == 0
    assert result.stdout_bytes == result.stderr_bytes == 2000000
    assert len(result.stdout.encode()) == 4096
    assert len(result.stderr.encode()) == 4096
    assert result.output_truncated is True
    assert result.stdout == "A" * 4096
    assert result.stderr == "B" * 4096


def test_bash_data_and_text_never_echo_entire_large_output(tmp_path):
    tool = BashTool(project_root=tmp_path)
    result = json.loads(tool.run({
        "command": 'python -c "print(chr(88)*250000,end=chr(0)[:0])"',
        "timeout_ms": 8000,
    }))
    assert result["status"] == "success"
    assert result["data"]["truncated"] is True
    assert len(result["data"]["stdout"].encode()) <= tool.OUTPUT_MAX_BYTES
    assert result["stats"]["stdout_bytes"] == 250000
    assert "Output truncated" in result["text"]
    assert len(json.dumps(result)) < 70000


def test_bash_max_lines_applies_to_data_not_only_text(tmp_path):
    tool = BashTool(project_root=tmp_path)
    massive = json.loads(tool.run({
        "command": 'python -c "print((chr(90)+chr(10))*5000,end=chr(0)[:0])"',
    }))
    assert massive["status"] == "success"
    assert massive["data"]["truncated"] is True
    assert len(massive["data"]["stdout"].splitlines()) <= tool.OUTPUT_MAX_LINES


def test_runner_timeout_with_flooded_pipes_stops_process(tmp_path):
    result = run_process(
        _python_script("import os,time; os.write(1,b'Z'*800000); time.sleep(5)"),
        shell=False, cwd=str(tmp_path), env=os.environ.copy(), timeout=0.3,
        poll_interval=0.05, max_output_bytes=512,
    )
    assert result.timed_out is True
    assert result.output_truncated is True
    assert len(result.stdout) <= 512


def test_runner_interrupt_with_flooded_pipes_stops_process(tmp_path):
    event = threading.Event()
    holder = []

    def execute():
        holder.append(run_process(
            _python_script("import os,time; os.write(1,b'Q'*500000); time.sleep(10)"),
            shell=False, cwd=str(tmp_path), env=os.environ.copy(), timeout=8,
            cancel_event=event, poll_interval=0.05, max_output_bytes=256,
        ))

    worker = threading.Thread(target=execute)
    worker.start()
    time.sleep(0.25)
    event.set()
    worker.join(timeout=3)
    assert not worker.is_alive(), "reader/cancel path blocked the agent"
    assert holder[0].interrupted is True
    assert len(holder[0].stdout) <= 256


def test_stdin_data_is_sent_once_without_holding_stdout(tmp_path):
    value = b"A" * 100000
    result = run_process(
        _python_script("import sys; data=sys.stdin.buffer.read(); print(len(data))"),
        shell=False, cwd=str(tmp_path), env=os.environ.copy(), timeout=4,
        stdin_data=value, max_output_bytes=128,
    )
    assert result.returncode == 0
    assert result.stdout.strip() == str(len(value))


@pytest.mark.parametrize("bad", [0, -1, True, "100", None])
def test_invalid_capture_limits_rejected_before_spawn(tmp_path, bad):
    with pytest.raises(ValueError, match="max_output_bytes"):
        run_process(
            _python_script("print('never start')"),
            shell=False, cwd=str(tmp_path), env=os.environ.copy(),
            timeout=3, max_output_bytes=bad,
        )


def test_invalid_bash_args_are_errors_and_never_execute(tmp_path):
    tool = BashTool(project_root=tmp_path)
    for parameters in [
        {"command": "python -V", "directory": []},
        {"command": "python -V", "directory": 4},
        {"command": "python -V", "directory": "\x00"},
        {"command": "python -V", "timeout_ms": True},
        {"command": "x" * (tool.MAX_COMMAND_CHARS + 1)},
    ]:
        result = json.loads(tool.run(parameters))
        assert result["status"] == "error"
        assert result["error"]["code"] == "INVALID_PARAM"
