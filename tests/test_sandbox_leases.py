"""Signed Docker lease ownership and bounded orphan reconciliation."""
from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path

import pytest

from core.sandbox.docker_lifecycle import (
    DockerLeaseManager, SandboxLeaseError, MAX_SCAN, EXPIRES_LABEL,
    OWNER_LABEL, SIGNATURE_LABEL, VERSION_LABEL, VERSION,
    load_installation_key,
)
from core.sandbox.docker_provider import DockerCommandProvider, SandboxUnavailable


class FakeDocker:
    def __init__(self):
        self.containers: dict[str, tuple[str, dict[str, str]]] = {}
        self.calls: list[list[str]] = []
        self.fail_ps = False

    def add(self, index: int, name: str, labels: dict[str, str]):
        cid = f"{index:012x}"
        self.containers[cid] = (name, labels)
        return cid

    def __call__(self, argv, **kwargs):
        args = list(argv)
        self.calls.append(args)
        assert kwargs["check"] is False and kwargs["timeout"] <= 2.0
        assert kwargs["capture_output"] and kwargs["text"]
        if args[1] == "ps":
            if self.fail_ps:
                raise OSError("unavailable")
            filters = [args[i+1] for i, arg in enumerate(args[:-1]) if arg == "--filter"]
            eligible = [
                cid for cid, (name, labels) in self.containers.items()
                if all(item.removeprefix("label=").partition("=")[0] in labels
                       and labels[item.removeprefix("label=").partition("=")[0]]
                       == item.removeprefix("label=").partition("=")[2] for item in filters)
            ][:MAX_SCAN]
            return subprocess.CompletedProcess(args, 0, "\n".join(eligible) + "\n", "")
        if args[1] == "inspect":
            cid = args[-1]
            if cid not in self.containers:
                return subprocess.CompletedProcess(args, 1, "", "missing")
            name, labels = self.containers[cid]
            import json
            return subprocess.CompletedProcess(
                args, 0, json.dumps(labels) + "|/" + name + "\n", "",
            )
        if args[1] == "rm":
            cid = args[-1]
            if cid not in self.containers:
                return subprocess.CompletedProcess(args, 1, "", "missing")
            del self.containers[cid]
            return subprocess.CompletedProcess(args, 0, cid, "")
        raise AssertionError("unexpected Docker command")


def _manager(fake: FakeDocker, now: list[int], key: bytes = b"a" * 32):
    return DockerLeaseManager(
        "/usr/bin/docker", key, runner=fake, clock=lambda: now[0],
    )


def _name(index):
    return f"wodeagent-{index:020x}"


def test_reaps_only_verified_expired_owned_containers():
    fake = FakeDocker()
    now = [1_800_000_000]
    owner = _manager(fake, now)
    other = _manager(fake, now, b"b" * 32)
    expired = fake.add(1, _name(1), owner.labels(_name(1), 10))
    fresh = fake.add(2, _name(2), owner.labels(_name(2), 600))
    foreign = fake.add(3, _name(3), other.labels(_name(3), 10))
    tampered_labels = owner.labels(_name(4), 10)
    tampered_labels[EXPIRES_LABEL] = str(now[0] - 1)
    spoofed = fake.add(4, _name(4), tampered_labels)
    copied_labels = owner.labels(_name(5), 10)
    impersonator = fake.add(5, _name(6), copied_labels)
    unsigned = fake.add(6, _name(7), {
        OWNER_LABEL: owner.owner, VERSION_LABEL: VERSION,
        EXPIRES_LABEL: str(now[0] - 1), SIGNATURE_LABEL: "0" * 64,
    })
    unowned = fake.add(7, _name(8), {})
    now[0] += 200  # short 10s lease expired; 600s one still live

    result = owner.reap()
    assert result.removed == 1
    assert expired not in fake.containers
    assert all(x in fake.containers for x in (
        fresh, foreign, spoofed, impersonator, unsigned, unowned,
    ))
    assert not any(x[1] == "rm" and x[-1] != expired for x in fake.calls)
    assert owner.reap().removed == 0


def test_active_lease_never_removed_even_after_expiry():
    fake = FakeDocker()
    now = [1_800_000_000]
    mgr = _manager(fake, now)
    name = _name(9)
    cid = fake.add(9, name, mgr.labels(name, 1))
    now[0] += 500
    mgr.activate(name)
    assert mgr.reap().removed == 0
    assert cid in fake.containers
    mgr.deactivate(name)
    assert mgr.reap().removed == 1
    assert cid not in fake.containers


def test_restarting_same_installation_can_reconcile_orphans():
    fake = FakeDocker()
    now = [1_800_000_000]
    original = _manager(fake, now)
    name = _name(10)
    cid = fake.add(10, name, original.labels(name, 1))
    now[0] += 500
    recovered = _manager(fake, now)
    assert recovered.owner == original.owner
    assert recovered.reap().removed == 1
    assert cid not in fake.containers


def test_bounded_reap_does_not_inspect_unbounded_container_list():
    fake = FakeDocker()
    now = [1_800_000_000]
    mgr = _manager(fake, now)
    for index in range(1, 100):
        name = _name(index)
        fake.add(index, name, mgr.labels(name, 1))
    now[0] += 500
    result = mgr.reap()
    assert result.inspected <= MAX_SCAN
    assert result.removed <= MAX_SCAN
    assert len(fake.containers) >= 99 - MAX_SCAN


def test_missing_daemon_fails_safely_without_removing_any_container():
    fake = FakeDocker()
    now = [1_800_000_000]
    mgr = _manager(fake, now)
    cid = fake.add(11, _name(11), mgr.labels(_name(11), 1))
    fake.fail_ps = True
    assert mgr.reap().failures == 1
    assert cid in fake.containers
    assert not any(call[1] == "rm" for call in fake.calls)


def test_concurrent_live_commands_create_distinct_signed_labels(tmp_path):
    fake = FakeDocker()
    now = [1_800_000_000]
    mgr = _manager(fake, now)
    provider = DockerCommandProvider(
        tmp_path, docker_binary="/usr/bin/docker",
        runner=lambda a, **kw: subprocess.CompletedProcess(a, 0, "", ""),
        lease_manager=mgr,
    )
    first, first_name = provider.command_argv("echo first")
    second, second_name = provider.command_argv("echo second")
    assert first_name != second_name
    assert first[first.index("--name") + 1] == first_name
    assert "--label" in first
    assert "--network=none" in first and "--read-only" in second
    def labels(argv):
        return dict(argv[i+1].split("=", 1)
                    for i, x in enumerate(argv[:-1]) if x == "--label")
    left, right = labels(first), labels(second)
    assert left[OWNER_LABEL] == right[OWNER_LABEL] == mgr.owner
    assert left[SIGNATURE_LABEL] != right[SIGNATURE_LABEL]
    assert mgr._eligible(first_name, left) is False  # Not expired


@pytest.mark.skipif(os.name != "posix", reason="requires POSIX permissions")
def test_private_persistent_owner_key_and_symlink_rejection(tmp_path):
    path = tmp_path / "owners" / "key"
    first = load_installation_key(path)
    assert len(first) == 32
    assert load_installation_key(path) == first
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700

    path.chmod(0o644)
    with pytest.raises(SandboxLeaseError):
        load_installation_key(path)
    path.unlink()
    target = tmp_path / "target"
    target.write_bytes(b"x" * 32)
    path.symlink_to(target)
    with pytest.raises(SandboxLeaseError):
        load_installation_key(path)
    assert target.read_bytes() == b"x" * 32


def test_owner_key_must_not_be_mounted_in_workspace(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    secret = workspace / "private.key"
    workspace.mkdir()
    monkeypatch.setattr(
        "core.sandbox.docker_provider.installation_key_path", lambda: secret,
    )
    with pytest.raises(SandboxUnavailable, match="expose"):
        DockerCommandProvider(workspace, docker_binary="/usr/bin/docker")
