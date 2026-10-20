"""Subprocess output limits are enforced while reading, not after allocation."""
from __future__ import annotations

import json
import os
import sys

from core.runtime.process_execution import MAX_CAPTURE_BYTES, run_process
from core.sandbox.command_tool import SandboxBashTool
from core.sandbox.contracts import CommandResult
from tools.builtin.bash import BashTool


def test_large_stdout_and_stderr_do_not_expand_capture(tmp_path):
    outcome = run_process(
        [sys.executable, "-c",
         "import sys; print('a'*200000); print('b'*200000,file=sys.stderr)"],
        shell=False, cwd=str(tmp_path), env=os.environ.copy(), timeout=10,
    )
    assert outcome.returncode == 0
    assert outcome.stdout_truncated and outcome.stderr_truncated
    assert len(outcome.stdout.encode()) <= MAX_CAPTURE_BYTES
    assert len(outcome.stderr.encode()) <= MAX_CAPTURE_BYTES
    assert outcome.stdout.startswith("aaaa")
    assert outcome.stderr.startswith("bbbb")


def test_small_output_and_stdin_bytes_remain_exact(tmp_path):
    data = b"\x00\xffABC" * 2000
    result = run_process(
        [sys.executable, "-c",
         "import sys; b=sys.stdin.buffer.read(); "
         "sys.stdout.write(str(len(b)))"],
        shell=False, cwd=str(tmp_path), env=os.environ.copy(), timeout=10,
        stdin_data=data,
    )
    assert result.returncode == 0
    assert result.stdout == str(len(data))
    assert not result.stdout_truncated
    assert not result.stderr_truncated


def test_bash_tool_marks_process_level_overflow_as_truncated(tmp_path):
    tool = BashTool(project_root=tmp_path)
    result = json.loads(tool.run({
        "command": "python -c \"print('x' * 150000)\"",
        "timeout_ms": 10000,
    }))
    assert result["data"]["truncated"] is True
    assert len(result["data"]["stdout"].encode()) <= MAX_CAPTURE_BYTES


def test_sandbox_tool_propagates_pretruncated_provider_output(tmp_path):
    class FakeProvider:
        kind = "docker"
        workspace = tmp_path

        def execute(self, *args, **kwargs):
            return CommandResult("short captured prefix", "", 0, truncated=True)

    result = json.loads(SandboxBashTool(FakeProvider()).run({"command": "echo ok"}))
    assert result["status"] == "partial"
    assert result["data"]["truncated"] is True
