"""Actual Docker containment smoke test; opt-in for trusted CI runners only."""
import os
import threading
import time
from pathlib import Path

import pytest

from core.sandbox.docker_provider import DockerCommandProvider


pytestmark = pytest.mark.skipif(
    os.environ.get("AGENT_DOCKER_INTEGRATION") != "1",
    reason="requires Docker daemon and preloaded python:3.12-alpine image",
)


def test_workspace_isolated_nonroot_and_networkless(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    secret = tmp_path / "host-private.txt"
    secret.write_text("NOT_IN_CONTAINER")
    provider = DockerCommandProvider(root)
    inside = provider.execute("pwd; id -u; printf verified > verify.txt")
    assert inside.exit_code == 0, inside.stderr
    lines = inside.stdout.splitlines()
    assert lines[0] == "/workspace"
    assert int(lines[1]) != 0
    assert (root / "verify.txt").read_text() == "verified"

    outside = provider.execute("test ! -e /workspace/../host-private.txt")
    assert outside.exit_code == 0, outside.stderr

    net = provider.execute(
        """python -c 'import socket
try:
    socket.create_connection(("1.1.1.1", 443), timeout=1)
except OSError:
    print("DENIED")
else:
    raise SystemExit("network access unexpectedly succeeded")'""",
        timeout_seconds=12,
    )
    assert net.exit_code == 0, net.stderr
    assert "DENIED" in net.stdout


def test_running_container_is_stopped_on_turn_cancel(tmp_path):
    """On a real daemon, stop must end the tool before its 60s timeout."""
    root = tmp_path / "project"
    root.mkdir()
    started = root / "started.txt"
    cancelled = threading.Event()
    provider = DockerCommandProvider(root)
    results = []
    worker = threading.Thread(
        target=lambda: results.append(provider.execute(
            "printf ready > started.txt; sleep 60",
            timeout_seconds=65, cancel_event=cancelled,
        )),
    )
    worker.start()
    deadline = time.monotonic() + 15
    while not started.exists() and time.monotonic() < deadline and worker.is_alive():
        time.sleep(0.05)
    try:
        assert started.exists(), "container did not start in time"
    finally:
        cancelled.set()
        worker.join(timeout=15)
    assert not worker.is_alive(), "cancel failed to stop Docker command"
    assert results and results[0].interrupted and not results[0].timed_out
