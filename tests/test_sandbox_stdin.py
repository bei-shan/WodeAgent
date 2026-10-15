"""Binary stdin is private process data with bounded time and cancellation."""
from __future__ import annotations

import subprocess
import sys
import threading
import time

import pytest

from core.runtime.process_execution import run_process
from core.sandbox.docker_provider import DockerCommandProvider
from core.sandbox.contracts import SandboxUnavailable


def test_binary_stdin_pipe_roundtrip_without_command_args(tmp_path):
    payload = b"\x00\xffDATA\x01" * 8192
    cmd = [
        sys.executable, "-c",
        "import sys,base64; print(base64.b64encode(sys.stdin.buffer.read()).decode())",
    ]
    result = run_process(
        cmd, shell=False, cwd=str(tmp_path), env={},
        timeout=5, stdin_data=payload,
    )
    import base64
    assert result.returncode == 0
    assert base64.b64decode(result.stdout.strip()) == payload
    assert not result.timed_out and not result.interrupted
    assert payload not in b" ".join(arg.encode() for arg in cmd)


def test_binary_stdin_timeout_kills_long_running_child(tmp_path):
    cmd = [
        sys.executable, "-c",
        "import sys,time; sys.stdin.buffer.read(1); time.sleep(30)",
    ]
    result = run_process(
        cmd, shell=False, cwd=str(tmp_path), env={},
        timeout=0.25, stdin_data=b"protected",
    )
    assert result.timed_out and not result.interrupted


def test_binary_stdin_cancel_ends_wait_without_deadlock(tmp_path):
    cancel = threading.Event()
    cmd = [
        sys.executable, "-c",
        "import sys,time; sys.stdin.buffer.read(1); time.sleep(30)",
    ]
    results = []
    worker = threading.Thread(target=lambda: results.append(run_process(
        cmd, shell=False, cwd=str(tmp_path), env={}, timeout=12,
        cancel_event=cancel, stdin_data=b"secret",
    )))
    worker.start()
    time.sleep(0.25)
    cancel.set()
    worker.join(timeout=4)
    assert not worker.is_alive()
    assert results[0].interrupted and not results[0].timed_out


def test_pre_cancelled_binary_payload_does_not_spawn(tmp_path):
    event = threading.Event()
    event.set()
    result = run_process(
        [sys.executable, "-c", "raise AssertionError('spawned')"],
        shell=False, cwd=str(tmp_path), env={}, timeout=2,
        cancel_event=event, stdin_data=b"secret",
    )
    assert result.interrupted and result.returncode is None


@pytest.mark.parametrize("bad", [None, "secret", bytearray(b"secret")])
def test_input_contract_rejects_unsupported_payload(tmp_path, bad):
    if bad is None:
        assert run_process(
            [sys.executable, "-c", "print('ok')"],
            shell=False, cwd=str(tmp_path), env={}, timeout=3,
            stdin_data=bad,
        ).returncode == 0
    else:
        with pytest.raises(ValueError):
            run_process(
                [sys.executable, "-c", "print('bad')"],
                shell=False, cwd=str(tmp_path), env={}, timeout=3,
                stdin_data=bad,
            )


def test_docker_runner_binary_stdin_is_only_in_input_not_argv(tmp_path):
    calls = []
    def fake_runner(argv, **kwargs):
        calls.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, 0, b"ok", b"")
    docker = DockerCommandProvider(
        tmp_path, docker_binary="/usr/bin/docker", runner=fake_runner,
    )
    secret = b"secret\x00not-in-command"
    result = docker.execute_stdin(
        "python3 -c 'import sys; sys.stdin.buffer.read()'", secret,
    )
    assert result.stdout == "ok"
    assert len(calls) == 1
    args, kwargs = calls[0]
    assert args[:3] == ["/usr/bin/docker", "run", "-i"]
    assert kwargs["input"] == secret
    assert kwargs["text"] is False
    assert secret not in " ".join(args).encode()
    assert "--network=none" in args and "--read-only" in args


def test_docker_rejects_oversized_stdin_before_invocation(tmp_path):
    called = []
    docker = DockerCommandProvider(
        tmp_path, docker_binary="/usr/bin/docker",
        runner=lambda argv, **kw: called.append(argv),
    )
    with pytest.raises(ValueError):
        docker.execute_stdin("cat", b"x" * 65537)
    assert not called
