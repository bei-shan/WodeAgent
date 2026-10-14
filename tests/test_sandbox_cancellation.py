"""Docker cancellation must stop the CLI, request daemon cleanup, and fail closed."""
from __future__ import annotations

import json
import subprocess
import threading

import pytest

from core.runtime.cancellation import scoped_tool_cancellation
from core.runtime.process_execution import ProcessOutcome
from core.sandbox.command_tool import SandboxBashTool
from core.sandbox.docker_provider import DockerCommandProvider, SandboxUnavailable


def _provider(tmp_path, runner=None):
    return DockerCommandProvider(
        tmp_path, docker_binary="/usr/bin/docker", runner=runner,
    )


def test_cancelled_before_start_never_invokes_docker(tmp_path):
    calls = []
    event = threading.Event()
    event.set()

    def runner(*args, **kwargs):
        calls.append(args)
        raise AssertionError("cancelled command must not start")

    tool = SandboxBashTool(_provider(tmp_path, runner))
    with scoped_tool_cancellation(event):
        result = json.loads(tool.run({"command": "echo should-not-run"}))

    assert result["status"] == "error"
    assert result["error"]["code"] == "INTERRUPTED"
    assert not calls


def test_cancelled_during_run_kills_cli_and_removes_daemon_container(tmp_path, monkeypatch):
    cancelled = threading.Event()
    running = threading.Event()
    cleanup_calls = []
    started_argv = []
    outcomes = []

    def fake_run_process(argv, *, shell, cwd, env, timeout, cancel_event):
        started_argv.append(list(argv))
        assert shell is False
        assert cancel_event is cancelled
        assert timeout == 60
        running.set()
        assert cancelled.wait(timeout=3), "test never delivered stop signal"
        # run_process has killed its POSIX process group at this point.
        return ProcessOutcome("partial", "", None, interrupted=True)

    def cleanup(argv, **kwargs):
        cleanup_calls.append(list(argv))
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr("core.sandbox.docker_provider.run_process", fake_run_process)
    provider = _provider(tmp_path)
    provider._run = cleanup
    tool = SandboxBashTool(provider)

    def invoke():
        with scoped_tool_cancellation(cancelled):
            outcomes.append(json.loads(tool.run({"command": "sleep 60", "timeout_ms": 60000})))

    worker = threading.Thread(target=invoke)
    worker.start()
    assert running.wait(timeout=3), "Docker command never started"
    cancelled.set()
    worker.join(timeout=3)

    assert not worker.is_alive(), "cancelled tool remained blocked"
    assert outcomes[0]["status"] == "error"
    assert outcomes[0]["error"]["code"] == "INTERRUPTED"
    assert len(cleanup_calls) == 1
    assert cleanup_calls[0][:3] == ["/usr/bin/docker", "rm", "--force"]
    assert cleanup_calls[0][3] == started_argv[0][started_argv[0].index("--name") + 1]


def test_timeout_in_default_process_path_still_removes_container(tmp_path, monkeypatch):
    cleanup_calls = []
    monkeypatch.setattr(
        "core.sandbox.docker_provider.run_process",
        lambda *a, **k: ProcessOutcome("partial", "", None, timed_out=True),
    )
    provider = _provider(tmp_path)
    provider._run = lambda argv, **kw: (
        cleanup_calls.append(list(argv)) or subprocess.CompletedProcess(argv, 0, "", "")
    )
    outcome = provider.execute("sleep 60", timeout_seconds=0.1)
    assert outcome.timed_out and not outcome.interrupted
    assert outcome.stdout == "partial"
    assert cleanup_calls[0][1:3] == ["rm", "--force"]


@pytest.mark.parametrize("cpus", [float("nan"), float("inf"), -1, 0, True])
def test_invalid_cpu_budget_fails_closed(tmp_path, cpus):
    with pytest.raises(SandboxUnavailable):
        DockerCommandProvider(tmp_path, docker_binary="/usr/bin/docker", cpus=cpus)


@pytest.mark.parametrize("timeout", [float("nan"), float("inf"), -1, 0, True])
def test_invalid_docker_timeouts_rejected(tmp_path, timeout):
    with pytest.raises(ValueError):
        _provider(tmp_path).execute("echo ok", timeout_seconds=timeout)
