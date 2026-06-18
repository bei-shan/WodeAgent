"""PermissionGate unit tests.

Run:
    python -m pytest tests/test_permission_gate.py -v
"""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch

import pytest

from tools.permission_gate import PermissionGate, _is_always_denied, create_permission_gate


# ---------------------------------------------------------------------------
# _is_always_denied
# ---------------------------------------------------------------------------


def test_is_always_denied_unix_shadow():
    assert _is_always_denied("/etc/shadow") is True


def test_is_always_denied_unix_sudoers():
    assert _is_always_denied("/etc/sudoers") is True


def test_is_always_denied_windows_system32():
    assert _is_always_denied("C:\\Windows\\System32\\drivers\\etc") is True


def test_is_always_denied_ssh_key():
    assert _is_always_denied("/home/user/.ssh/id_rsa") is True


def test_is_always_denied_normal_path():
    assert _is_always_denied("/home/user/projects/myapp/src/main.py") is False


def test_is_always_denied_project_file():
    assert _is_always_denied("/tmp/log.txt") is False


# ---------------------------------------------------------------------------
# PermissionGate basic
# ---------------------------------------------------------------------------


def test_is_within_root(tmp_path: Path):
    gate = PermissionGate(project_root=tmp_path)
    inside = str(tmp_path / "src" / "main.py")
    outside = str(Path("/etc/hosts"))
    assert gate.is_within_root(inside) is True
    assert gate.is_within_root(outside) is False


def test_check_inside_root_returns_granted(tmp_path: Path):
    gate = PermissionGate(project_root=tmp_path)
    result = gate.check(str(tmp_path / "file.txt"))
    assert result == "granted"


def test_check_always_denied(tmp_path: Path):
    gate = PermissionGate(project_root=tmp_path)
    result = gate.check("/etc/shadow")
    assert result == "denied"


def test_check_outside_root_returns_ask(tmp_path: Path):
    gate = PermissionGate(project_root=tmp_path, interactive=True)
    result = gate.check("/tmp/other.txt")
    assert result == "ask"


def test_check_outside_root_non_interactive_returns_denied(tmp_path: Path):
    gate = PermissionGate(project_root=tmp_path, interactive=False)
    result = gate.check("/tmp/other.txt")
    assert result == "denied"


def test_check_hard_sandbox_returns_denied(tmp_path: Path):
    gate = PermissionGate(project_root=tmp_path, soft_sandbox=False)
    result = gate.check("/tmp/other.txt")
    assert result == "denied"


# ---------------------------------------------------------------------------
# PermissionGate cache
# ---------------------------------------------------------------------------


def test_cache_granted_decision(tmp_path: Path):
    gate = PermissionGate(project_root=tmp_path)
    gate._cache_decision("/tmp/log.txt", "granted")
    assert gate.check("/tmp/log.txt") == "granted"


def test_cache_denied_decision(tmp_path: Path):
    gate = PermissionGate(project_root=tmp_path)
    gate._cache_decision("/tmp/log.txt", "denied")
    assert gate.check("/tmp/log.txt") == "denied"


def test_grant_method(tmp_path: Path):
    gate = PermissionGate(project_root=tmp_path)
    gate.grant("/tmp/log.txt")
    assert gate.check("/tmp/log.txt") == "granted"


def test_clear_cache(tmp_path: Path):
    gate = PermissionGate(project_root=tmp_path)
    gate.grant("/tmp/log.txt")
    gate.clear_cache()
    assert gate.check("/tmp/log.txt") == "ask"


def test_cache_eviction(tmp_path: Path):
    gate = PermissionGate(project_root=tmp_path, cache_size=3)
    gate._cache["/x/1"] = "granted"
    gate._cache["/x/2"] = "granted"
    gate._cache["/x/3"] = "granted"
    assert len(gate._cache) == 3
    gate._cache_decision("/x/4", "granted")  # should evict "/x/1"
    assert len(gate._cache) == 3
    assert "/x/1" not in gate._cache  # evicted (oldest)
    assert "/x/4" in gate._cache       # new entry


# ---------------------------------------------------------------------------
# PermissionGate ask (non-interactive)
# ---------------------------------------------------------------------------


def test_ask_non_interactive_returns_denied(tmp_path: Path):
    gate = PermissionGate(project_root=tmp_path, interactive=False)
    result = gate.ask("/tmp/other.txt", "Read", "读取")
    assert result == "denied"
    assert gate.check("/tmp/other.txt") == "denied"


# ---------------------------------------------------------------------------
# Subagent gate
# ---------------------------------------------------------------------------


def test_subagent_gate_shares_cache(tmp_path: Path):
    main_gate = PermissionGate(project_root=tmp_path, interactive=True)
    main_gate.grant("/tmp/shared.txt")

    sub_gate = main_gate.subagent_gate()
    assert sub_gate.check("/tmp/shared.txt") == "granted"
    # Sub-agent should not be interactive
    result = sub_gate.ask("/tmp/new.txt", "Read", "读取")
    assert result == "denied"


def test_subagent_gate_new_path_denied(tmp_path: Path):
    main_gate = PermissionGate(project_root=tmp_path, interactive=True)
    sub_gate = main_gate.subagent_gate()
    # New path not in cache → denied for sub-agent
    assert sub_gate.check("/tmp/new.txt") == "denied"


# ---------------------------------------------------------------------------
# create_permission_gate factory
# ---------------------------------------------------------------------------


def test_create_permission_gate_defaults(tmp_path: Path):
    gate = create_permission_gate(str(tmp_path))
    assert gate._soft_sandbox is True
    assert gate._interactive is True


@patch.dict(os.environ, {"PERMISSION_SOFT_SANDBOX": "false"}, clear=False)
def test_create_permission_gate_hard_sandbox(tmp_path: Path):
    gate = create_permission_gate(str(tmp_path))
    assert gate._soft_sandbox is False


@patch.dict(os.environ, {"AGENT_INTERACTIVE": "false"}, clear=False)
def test_create_permission_gate_non_interactive(tmp_path: Path):
    gate = create_permission_gate(str(tmp_path))
    assert gate._interactive is False
