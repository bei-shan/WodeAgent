"""Guest filesystem must never silently use host file tools or follow symlinks."""
from __future__ import annotations

import base64
import json
from threading import Event

import pytest

from core.sandbox.contracts import CommandResult, SandboxUnavailable
from core.sandbox.guest_files import GuestFilesystem, MAX_FILE_BYTES


class FakeGuest:
    kind = "boxlite"

    def __init__(self, tmp_path, result=None):
        self.workspace = tmp_path
        self.calls = []
        self.stdin_calls = []
        self.result = result or CommandResult(
            json.dumps({"ok": True, "data": base64.b64encode(b"guest-data").decode()}),
            "", 0,
        )

    def execute(self, command, *, timeout_seconds, cancel_event):
        self.calls.append((command, timeout_seconds, cancel_event))
        return self.result

    def execute_stdin(self, command, stdin_data, *, timeout_seconds, cancel_event):
        self.stdin_calls.append((command, stdin_data, timeout_seconds, cancel_event))
        return self.result


def test_file_read_via_guest_command_not_host(tmp_path):
    fake = FakeGuest(tmp_path)
    fs = GuestFilesystem(fake)
    assert fs.read_file("sub/example.bin") == b"guest-data"
    assert len(fake.calls) == 1
    command, timeout, event = fake.calls[0]
    assert command.startswith("exec python3 -c ")
    assert "sub/example.bin" in command
    assert "/workspace/" in command
    assert timeout == 30


@pytest.mark.parametrize("bad", [
    "../escape", "a/../escape", "/etc/passwd", "/workspace/../escape",
    "a/./b", "/workspaces/a", "", "x\x00y", "foo\\bar", "line\nend",
])
def test_guest_host_validation_rejects_traversal_without_dispatch(tmp_path, bad):
    fake = FakeGuest(tmp_path)
    fs = GuestFilesystem(fake)
    with pytest.raises((ValueError, PermissionError)):
        fs.read_file(bad)
    assert fake.calls == []


def test_guest_binary_write_size_and_acknowledgement(tmp_path):
    fake = FakeGuest(
        tmp_path, CommandResult('{"ok":true,"written":3}', "", 0),
    )
    fs = GuestFilesystem(fake)
    assert fs.write_file("file.txt", b"\x00\xff\x01") == 3
    assert len(fake.stdin_calls) == 1
    assert fake.stdin_calls[0][1] == b"\x00\xff\x01"
    assert base64.b64encode(b"\x00\xff\x01").decode() not in fake.stdin_calls[0][0]
    assert len(fake.calls) == 0
    with pytest.raises(ValueError):
        fs.write_file("file.txt", b"x" * (MAX_FILE_BYTES + 1))
    assert len(fake.stdin_calls) == 1


def test_guest_list_is_bounded_and_result_only(tmp_path):
    fake = FakeGuest(
        tmp_path, CommandResult('{"ok":true,"entries":["alpha","beta"]}', "", 0),
    )
    fs = GuestFilesystem(fake)
    assert fs.list_dir(".") == ["alpha", "beta"]
    assert "/workspace" in fake.calls[0][0]
    fake.result = CommandResult(
        json.dumps({"ok": True, "entries": [str(i) for i in range(129)]}),
        "", 0,
    )
    with pytest.raises(SandboxUnavailable):
        fs.list_dir("/workspace")


@pytest.mark.parametrize("outcome,error", [
    (CommandResult("", "", None, timed_out=True), TimeoutError),
    (CommandResult("", "", None, interrupted=True), InterruptedError),
    (CommandResult("garbage", "", 0), SandboxUnavailable),
    (CommandResult('{"ok":false,"error":"ACCESS_DENIED_OR_LIMIT"}', "", 0), PermissionError),
    (CommandResult('{"ok":true,"data":"not-base64!"}', "", 0), SandboxUnavailable),
    (CommandResult('{"ok":true}', "", 2), SandboxUnavailable),
])
def test_invalid_or_failed_guest_result_never_triggers_host_fallback(tmp_path, outcome, error):
    fake = FakeGuest(tmp_path, outcome)
    with pytest.raises(error):
        GuestFilesystem(fake).read_file("example.txt")
    assert len(fake.calls) == 1


def test_filesystem_requires_explicit_isolated_backend(tmp_path):
    fake = FakeGuest(tmp_path)
    fake.kind = "local_trusted"
    with pytest.raises(SandboxUnavailable):
        GuestFilesystem(fake)


def test_cancellation_forwarded_to_guest_command(tmp_path):
    fake = FakeGuest(tmp_path)
    event = Event()
    assert GuestFilesystem(fake).read_file("data.txt", cancel_event=event) == b"guest-data"
    assert fake.calls[0][2] is event


def test_boxlite_without_verified_stdin_fails_closed(tmp_path):
    class UnstreamedBox:
        kind = "boxlite"
        workspace = tmp_path
        def execute(self, command, *, timeout_seconds, cancel_event):
            raise AssertionError("must never send file bytes via generic execute")
    with pytest.raises(SandboxUnavailable, match="stdin"):
        GuestFilesystem(UnstreamedBox()).write_file("sensitive.txt", b"SECRET-STRING")


def test_sensitive_payload_never_enters_guest_command_args(tmp_path):
    fake = FakeGuest(
        tmp_path, CommandResult('{"ok":true,"written":19}', "", 0),
    )
    secret = b"secret-do-not-argv!"
    assert GuestFilesystem(fake).write_file("sensitive.txt", secret) == len(secret)
    command = fake.stdin_calls[0][0]
    assert secret.decode() not in command
    assert base64.b64encode(secret).decode() not in command
    assert fake.stdin_calls[0][1] == secret
