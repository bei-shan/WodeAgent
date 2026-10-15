"""Opt-in real BoxLite micro-VM containment smoke on supported KVM/HVF hosts.

Run only on a trusted Linux KVM or macOS HVF machine with the BoxLite SDK and
an approved/preloaded image:
  AGENT_BOXLITE_INTEGRATION=1 pytest tests/test_sandbox_boxlite_integration.py -q
"""
from __future__ import annotations

import os
import threading
import time

import pytest

from core.sandbox.boxlite_provider import BoxliteCommandProvider

pytestmark = pytest.mark.skipif(
    os.environ.get("AGENT_BOXLITE_INTEGRATION") != "1",
    reason="requires real KVM/HVF hardware and optional BoxLite runtime",
)


def test_real_boxlite_workspace_and_no_network(tmp_path):
    workspace = tmp_path / "project"
    workspace.mkdir()
    secret = tmp_path / "private.txt"
    secret.write_text("not-shared")
    provider = BoxliteCommandProvider(workspace)
    result = provider.execute(
        "pwd; id -u; printf ok > output.txt; test ! -e /workspace/../private.txt",
        timeout_seconds=90,
    )
    assert result.exit_code == 0, result.stderr
    assert result.stdout.splitlines()[0] == "/workspace"
    assert int(result.stdout.splitlines()[1]) != 0
    assert (workspace / "output.txt").read_text() == "ok"
    network = provider.execute(
        """python -c 'import socket
try:
    socket.create_connection(("1.1.1.1", 443), 2)
except OSError:
    print("DENIED")
else:
    raise SystemExit("network not blocked")'""",
        timeout_seconds=90,
    )
    assert network.exit_code == 0, network.stderr
    assert "DENIED" in network.stdout


def test_real_boxlite_stop_cancels_guest_exec(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    cancelled = threading.Event()
    provider = BoxliteCommandProvider(root)
    outcomes = []
    worker = threading.Thread(target=lambda: outcomes.append(
        provider.execute(
            "printf ready > start.txt; sleep 60",
            timeout_seconds=100, cancel_event=cancelled,
        ),
    ))
    worker.start()
    deadline = time.monotonic() + 50
    while not (root / "start.txt").exists() and time.monotonic() < deadline and worker.is_alive():
        time.sleep(0.1)
    try:
        assert (root / "start.txt").exists(), "guest did not start"
    finally:
        cancelled.set()
        worker.join(timeout=15)
    assert not worker.is_alive(), "guest cleanup did not complete"
    assert outcomes and outcomes[0].interrupted
