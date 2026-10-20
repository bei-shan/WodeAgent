"""Real Docker acceptance for trusted hosted leased command execution.

Not full LLM Agent execution. This is a real guest process and fresh empty
tenant-independent sandbox, with no network, no inherited project files.
"""
from __future__ import annotations

import os
import pytest
from threading import Event
from pathlib import Path

from core.memory.contracts import MemoryScope
from core.persistence.postgres_turn_queue import LeasedTurn
from core.runtime.hosted_docker_runner import DockerHostedTurnRunner
from core.runtime.tool_authorization import HostedToolAuthority


pytestmark = pytest.mark.skipif(
    os.environ.get("AGENT_DOCKER_INTEGRATION") != "1",
    reason="requires real Docker daemon and approved preloaded image",
)


def test_real_hosted_guest_cannot_see_host_workspace_and_receives_no_network(tmp_path):
    secret = tmp_path / "host-secret.txt"
    secret.write_text("DO_NOT_MOUNT")
    scope = MemoryScope("tenant-A", "user-A", "project-A")
    authority = HostedToolAuthority(scope, lambda requested: requested == scope)
    job = LeasedTurn(
        "aaaaaaaa-aaaa-4aaa-aaaa-aaaaaaaaaaaa",
        {"kind": "docker-command",
         "command": (
            "test ! -e /workspace/host-secret.txt && "
            "test ! -e /run/docker.sock && "
            "python -c 'import socket; "
            "s=socket.socket(); "
            "s.settimeout(1); "
            "\ntry: s.connect((\"1.1.1.1\",443)); raise SystemExit(9)"
            "\nexcept OSError: print(\"ISOLATED\")'"
        ),
         "timeout_seconds": 20},
        7, 1,
    )
    result = DockerHostedTurnRunner()(job, authority, Event())
    assert result["exit_code"] == 0, result
    assert result["stdout"].strip() == "ISOLATED"
    assert secret.read_text() == "DO_NOT_MOUNT"
