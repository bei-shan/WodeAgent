"""Real-hosted runner boundary: lease identity, exact jobs, guest-only workspace."""
from __future__ import annotations

from pathlib import Path
from threading import Event
from types import SimpleNamespace
import pytest

from core.memory.contracts import MemoryScope
from core.persistence.postgres_turn_queue import LeasedTurn
from core.runtime.hosted_docker_runner import DockerHostedTurnRunner
from core.runtime.hosted_worker import HostedLeaseLost
from core.runtime.tool_authorization import HostedToolAuthority
from core.sandbox.contracts import CommandResult


OWNER = MemoryScope("tenant-a", "user-a", "project-a")
turn = lambda cmd="echo ok", timeout=15: LeasedTurn(
    "aaaaaaaa-aaaa-4aaa-aaaa-aaaaaaaaaaaa",
    {"kind": "docker-command", "command": cmd, "timeout_seconds": timeout},
    7, 1,
)


class FakeDocker:
    def __init__(self, workspace, **kw):
        self.workspace = Path(workspace)
        self.closed = False
        self.call = None

    def execute(self, command, *, timeout_seconds, cancel_event):
        self.call = (command, timeout_seconds, cancel_event)
        assert self.workspace.is_dir()
        assert list(self.workspace.iterdir()) == [], "hosted guest must start EMPTY"
        return CommandResult("ok", "", 0)

    def close(self):
        self.closed = True


def test_runner_requires_authorized_lease_and_truly_ephemeral_workspace(tmp_path):
    instances = []
    def make(*a, **kw):
        provider = FakeDocker(*a, **kw)
        instances.append(provider)
        return provider

    enabled = {"allowed": True}
    auth = HostedToolAuthority(OWNER, lambda scope: enabled["allowed"])
    runner = DockerHostedTurnRunner(provider_factory=make)
    output = runner(turn(), auth, Event())
    assert output["stdout"] == "ok" and output["exit_code"] == 0
    assert instances[0].closed
    assert not instances[0].workspace.exists(), "ephemeral job file tree leaked"

    enabled["allowed"] = False
    with pytest.raises(HostedLeaseLost):
        runner(turn(), auth, Event())
    assert len(instances) == 1


def test_runner_rejects_unauthorized_payloads_and_cancel(tmp_path):
    calls = []
    def factory(*a, **kw):
        calls.append(True)
        return FakeDocker(*a, **kw)
    runner = DockerHostedTurnRunner(provider_factory=factory)
    authority = HostedToolAuthority(OWNER, lambda s: True)
    bad = [
        LeasedTurn("id", {"kind": "docker-command", "command": "ok", "timeout_seconds": 10, "workspace": "/host"}, 7, 1),
        LeasedTurn("id", {"kind": "host-command", "command": "ok", "timeout_seconds": 10}, 7, 1),
        turn("x" * 5000),
        turn(timeout=121),
    ]
    for job in bad:
        with pytest.raises(ValueError):
            runner(job, authority, Event())
    flag = Event()
    flag.set()
    with pytest.raises(HostedLeaseLost):
        runner(turn(), authority, flag)
    assert calls == []


def test_runner_denies_publication_after_revoked_membership(tmp_path):
    active = {"valid": True}
    def factory(*args, **kwargs):
        class Revoke(FakeDocker):
            def execute(self, *a, **kw):
                active["valid"] = False
                return CommandResult("secret", "", 0)
        return Revoke(*args, **kwargs)

    auth = HostedToolAuthority(OWNER, lambda scope: active["valid"])
    with pytest.raises(HostedLeaseLost):
        DockerHostedTurnRunner(provider_factory=factory)(turn(), auth, Event())


def test_runner_rejects_spoofed_authority():
    runner = DockerHostedTurnRunner(provider_factory=FakeDocker)
    with pytest.raises(Exception):
        runner(turn(), SimpleNamespace(allows=lambda *_: True), Event())
