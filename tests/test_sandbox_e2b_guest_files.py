"""E2B guest file reads are bounded, symlink-resistant and memory-only."""
from __future__ import annotations

import base64
import json
import subprocess
import sys
from types import SimpleNamespace

import pytest

from core.sandbox.contracts import CommandResult, SandboxUnavailable
from core.sandbox.e2b_guest_files import (
    E2BGuestFilesystem, MAX_REMOTE_FILE_BYTES, _GUEST_SCRIPT,
)


class FakeRemote:
    kind = "e2b"
    workspace = None

    def __init__(self, response):
        self.response = response
        self.calls = []

    def execute(self, command, directory=".", timeout_seconds=120, *, cancel_event=None):
        self.calls.append((command, directory, timeout_seconds, cancel_event))
        return self.response


def test_remote_binary_file_read_returns_memory_bytes_only(tmp_path):
    binary = b"\x00\xffremote\x01"
    remote = FakeRemote(CommandResult(
        json.dumps({"ok": True, "size": len(binary),
                    "data": base64.b64encode(binary).decode()}),
        "", 0,
    ))
    assert E2BGuestFilesystem(remote).read_file("nested/item.bin") == binary
    command, directory, timeout, _ = remote.calls[0]
    assert command.startswith("exec python3 -c ")
    assert "/home/user/nested/item.bin" in command
    assert directory == "/home/user" and timeout == 30
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("bad", [
    "../secret", "dir/../other", "/etc/passwd", "/workspace/file.txt",
    "/home/user/../etc", "foo//bar", "dir/./x", "foo\\bar",
    "", "x\x00y", "hello\nworld",
])
def test_invalid_guest_paths_never_contact_remote_sdk(bad):
    remote = FakeRemote(CommandResult("", "", 0))
    with pytest.raises((PermissionError, ValueError)):
        E2BGuestFilesystem(remote).read_file(bad)
    assert not remote.calls


def test_e2b_guest_dir_list_and_strict_size_limits():
    remote = FakeRemote(CommandResult(
        '{"ok":true,"entries":["alpha","beta"]}', "", 0,
    ))
    fs = E2BGuestFilesystem(remote)
    assert fs.list_dir(".") == ["alpha", "beta"]
    assert "/home/user" in remote.calls[0][0]
    remote.response = CommandResult(
        json.dumps({"ok": True, "entries": [str(i) for i in range(65)]}),
        "", 0,
    )
    with pytest.raises(SandboxUnavailable):
        fs.list_dir()
    remote.response = CommandResult(
        json.dumps({"ok": True, "entries": ["../bad"]}),
        "", 0,
    )
    with pytest.raises(SandboxUnavailable):
        fs.list_dir()


@pytest.mark.parametrize("outcome,expected", [
    (CommandResult("", "", None, interrupted=True), InterruptedError),
    (CommandResult("", "", None, timed_out=True), TimeoutError),
    (CommandResult("not-json", "", 0), SandboxUnavailable),
    (CommandResult('{"ok":false,"error":"ACCESS_DENIED_OR_LIMIT"}', "", 0), PermissionError),
    (CommandResult('{"ok":true,"size":4,"data":"!!!!"}', "", 0), SandboxUnavailable),
    (CommandResult('{"ok":true,"size":2,"data":"YQ=="}', "", 0), SandboxUnavailable),
    (CommandResult('{"ok":true}', "", 2), SandboxUnavailable),
])
def test_failure_is_redacted_and_never_local_fallback(outcome, expected):
    remote = FakeRemote(outcome)
    with pytest.raises(expected):
        E2BGuestFilesystem(remote).read_file("result.bin")
    assert len(remote.calls) == 1


def test_oversize_download_refused_and_remote_write_not_exposed():
    raw = b"X" * (MAX_REMOTE_FILE_BYTES + 1)
    remote = FakeRemote(CommandResult(
        json.dumps({"ok": True, "size": len(raw),
                    "data": base64.b64encode(raw).decode()}), "", 0,
    ))
    fs = E2BGuestFilesystem(remote)
    with pytest.raises(SandboxUnavailable):
        fs.read_file("too-large.txt")
    n_calls = len(remote.calls)
    with pytest.raises(SandboxUnavailable, match="separate authorization"):
        fs.write_file("sensitive.txt", b"PRIVATE")
    assert len(remote.calls) == n_calls


def test_remote_guest_files_reject_other_isolation_backends():
    remote = FakeRemote(CommandResult("", "", 0))
    remote.kind = "docker"
    with pytest.raises(SandboxUnavailable):
        E2BGuestFilesystem(remote)


def _execute_guest_script(root, action, target):
    # Simulate the *constant guest program* in an isolated temporary fixture
    # directory without calling any E2B API. The production root is fixed.
    script = _GUEST_SCRIPT.replace(
        'ROOT = "/home/user"', "ROOT = " + repr(str(root)),
    )
    result = subprocess.run(
        [sys.executable, "-c", script, action, str(target)],
        capture_output=True, text=True, timeout=3, check=False,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_actual_guest_dirfd_script_rejects_file_and_directory_symlink_pivots(tmp_path):
    root = tmp_path / "remote"
    root.mkdir()
    (root / "safe").mkdir()
    (root / "safe" / "file.bin").write_bytes(b"protected")
    secret = tmp_path / "outside.bin"
    secret.write_bytes(b"must-not-expose")
    (root / "safe" / "outside.bin").symlink_to(secret)
    (root / "alias").symlink_to(root / "safe", target_is_directory=True)

    success = _execute_guest_script(root, "read", root / "safe" / "file.bin")
    assert success["ok"] and base64.b64decode(success["data"]) == b"protected"
    assert not _execute_guest_script(
        root, "read", root / "safe" / "outside.bin",
    )["ok"]
    assert not _execute_guest_script(
        root, "read", root / "alias" / "file.bin",
    )["ok"]
    assert _execute_guest_script(root, "list", root / "safe")["ok"]
    assert secret.read_bytes() == b"must-not-expose"
