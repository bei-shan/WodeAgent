"""PermissionGate must authorize canonical targets, not path spellings."""
from __future__ import annotations

import threading

import pytest

from tools.permission_gate import PermissionGate


def test_path_aliases_share_one_decision(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    target = outside / "data.txt"
    target.write_text("x")
    alias = root / "alias"
    try:
        alias.symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unavailable")
    gate = PermissionGate(project_root=root)
    assert gate.check(str(alias / "data.txt")) == "ask"
    gate.grant(str(alias / "data.txt"))
    assert gate.check(str(target)) == "granted"
    assert gate.check(str(outside / ".." / "outside" / "data.txt")) == "granted"
    assert len(gate._cache) == 1


def test_sensitive_symlink_cannot_be_authorized_by_broker_or_grant(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    sensitive = tmp_path / ".ssh" / "id_rsa"
    sensitive.parent.mkdir()
    sensitive.write_text("secret")
    alias = root / "looks_safe"
    try:
        alias.symlink_to(sensitive)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unavailable")

    gate = PermissionGate(project_root=root)
    calls = []
    gate._broker = lambda *_: calls.append("asked") or "granted"
    assert gate.check(str(alias)) == "denied"
    assert gate.ask(str(alias), "Read", "read") == "denied"
    gate.grant(str(alias))
    assert gate.check(str(alias)) == "denied"
    assert not calls


def test_ask_rechecks_cached_decision_and_never_reprompts(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    target = tmp_path / "outside.txt"
    gate = PermissionGate(project_root=root)
    requested = []
    gate._broker = lambda *_: requested.append("prompt") or "granted"
    assert gate.ask(str(target), "Read", "read") == "granted"
    assert gate.ask(str(target), "Read", "read") == "granted"
    assert requested == ["prompt"]


def test_subagents_share_canonical_cache_and_lock(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    parent = PermissionGate(project_root=root)
    sub = parent.subagent_gate()
    target = tmp_path / "outside.txt"
    parent.grant(str(target))
    assert sub.check(str(target)) == "granted"
    assert sub._cache_lock is parent._cache_lock

    errors = []
    def writer():
        try:
            for index in range(100):
                parent.grant(str(tmp_path / f"file-{index}"))
                sub.check(str(target))
        except BaseException as exc:
            errors.append(exc)
    tasks = [threading.Thread(target=writer) for _ in range(4)]
    for task in tasks:
        task.start()
    for task in tasks:
        task.join()
    assert not errors
