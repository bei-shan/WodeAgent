"""Bounded guest-side filesystem operations for isolated Docker/BoxLite Bash.

No host filesystem API reads or writes model-targeted files. Path traversal
and symlink components are checked inside the guest with O_NOFOLLOW dirfds,
so swapping a workspace symlink between validation and access cannot redirect
the operation to another guest/host location.
"""
from __future__ import annotations

import base64
import json
import shlex
from threading import Event

from core.sandbox.contracts import SandboxCommandProvider, SandboxUnavailable

MAX_FILE_BYTES = 64 * 1024
MAX_DIR_ENTRIES = 128
MAX_RESULT_CHARS = 160 * 1024

# A single trusted script shipped as the Python -c argument *inside* the guest.
# Operator-provided content/path is never interpolated into its source code.
_GUEST_SCRIPT = r'''
import base64
import json
import os
import secrets
import stat
import sys

READ_LIMIT = 65536
DIR_LIMIT = 128
D = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW

def emit(data):
    print(json.dumps(data, ensure_ascii=True, separators=(",", ":")))

def operate():
    operation, path, payload = sys.argv[1:4]
    pieces = path.split("/")
    if (not path.startswith("/workspace/") and path != "/workspace") or any(
        p in (".", "..") for p in pieces
    ):
        raise PermissionError("invalid path")
    segments = path.removeprefix("/workspace").strip("/").split("/")
    if segments == [""]:
        segments = []
    if len(segments) > 32 or any(not part or len(part) > 255 for part in segments):
        raise PermissionError("invalid component")
    fd = os.open("/workspace", D)
    try:
        parents = segments if operation == "list" else segments[:-1]
        for part in parents:
            next_fd = os.open(part, D, dir_fd=fd)
            os.close(fd)
            fd = next_fd
        if operation == "list":
            entries = os.listdir(fd)
            if len(entries) > DIR_LIMIT:
                raise ValueError("directory too large")
            emit({"ok": True, "entries": sorted(entries)})
        elif operation == "read":
            if not segments:
                raise PermissionError("cannot read workspace root")
            handle = os.open(segments[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                             dir_fd=fd)
            try:
                info = os.fstat(handle)
                if not stat.S_ISREG(info.st_mode) or info.st_size > READ_LIMIT:
                    raise ValueError("unsupported file size/type")
                data = os.read(handle, READ_LIMIT + 1)
                if len(data) > READ_LIMIT:
                    raise ValueError("file grew during read")
                emit({"ok": True, "data": base64.b64encode(data).decode("ascii")})
            finally:
                os.close(handle)
        elif operation == "write":
            if not segments:
                raise PermissionError("cannot write workspace root")
            data = base64.b64decode(payload, validate=True)
            if len(data) > READ_LIMIT:
                raise ValueError("file too large")
            name = segments[-1]
            mode = 0o600
            try:
                info = os.stat(name, dir_fd=fd, follow_symlinks=False)
                if not stat.S_ISREG(info.st_mode):
                    raise PermissionError("not a regular file")
                # Do not unintentionally strip executable/read bits on updates;
                # never copy setuid/setgid/sticky bits from an old file.
                mode = stat.S_IMODE(info.st_mode) & 0o777
            except FileNotFoundError:
                pass
            temp = ".ridge-guest-" + secrets.token_hex(12)
            handle = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                             0o600, dir_fd=fd)
            try:
                with os.fdopen(handle, "wb") as output:
                    os.fchmod(output.fileno(), mode)
                    output.write(data)
                    output.flush()
                    os.fsync(output.fileno())
                os.replace(temp, name, src_dir_fd=fd, dst_dir_fd=fd)
            finally:
                try:
                    os.unlink(temp, dir_fd=fd)
                except FileNotFoundError:
                    pass
            emit({"ok": True, "written": len(data)})
        else:
            raise PermissionError("invalid operation")
    finally:
        os.close(fd)

try:
    operate()
except (OSError, ValueError, PermissionError):
    emit({"ok": False, "error": "ACCESS_DENIED_OR_LIMIT"})
'''


def _guest_path(path: str) -> str:
    if not isinstance(path, str) or not path or len(path) > 1024 or "\x00" in path:
        raise ValueError("Guest path must be a bounded nonempty string")
    if "\\" in path or any(ord(c) < 32 or ord(c) == 127 for c in path):
        raise PermissionError("Guest path contains disallowed characters")
    parts = path.split("/")
    if any(p in ("..", ".") for p in parts):
        raise PermissionError("Guest path traversal is prohibited")
    if path.startswith("/"):
        if not (path == "/workspace" or path.startswith("/workspace/")):
            raise PermissionError("Guest path outside /workspace")
        relative = path.removeprefix("/workspace").strip("/")
    else:
        relative = path.strip("/")
    segments = [] if not relative else relative.split("/")
    if len(segments) > 32 or any(
        not x or len(x.encode("utf-8")) > 255 for x in segments
    ):
        raise PermissionError("Guest path component is invalid")
    return "/workspace" if not segments else "/workspace/" + "/".join(segments)


class GuestFilesystem:
    """Structured file API backed exclusively by selected isolated execution."""

    def __init__(self, provider: SandboxCommandProvider):
        if provider.kind not in ("docker", "boxlite"):
            raise SandboxUnavailable("Guest file operations require an isolated backend")
        self._provider = provider

    def _request(
        self, operation: str, path: str, payload: str = "",
        *, timeout_seconds: float = 30, cancel_event: Event | None = None,
    ) -> dict:
        guest_path = _guest_path(path)
        command = "exec python3 -c " + " ".join(
            shlex.quote(part) for part in (_GUEST_SCRIPT, operation, guest_path, payload)
        )
        outcome = self._provider.execute(
            command, timeout_seconds=timeout_seconds,
            cancel_event=cancel_event,
        )
        if outcome.interrupted:
            raise InterruptedError("Guest file operation interrupted")
        if outcome.timed_out:
            raise TimeoutError("Guest file operation timed out")
        if outcome.exit_code != 0 or len(outcome.stdout) > MAX_RESULT_CHARS:
            raise SandboxUnavailable("Guest filesystem operation failed closed")
        try:
            record = json.loads(outcome.stdout)
        except (TypeError, ValueError) as exc:
            raise SandboxUnavailable("Invalid guest filesystem response") from exc
        if not isinstance(record, dict):
            raise SandboxUnavailable("Invalid guest filesystem response")
        if record.get("ok") is not True:
            raise PermissionError("Guest file operation denied or exceeded limits")
        return record

    def read_file(
        self, path: str, *, timeout_seconds: float = 30,
        cancel_event: Event | None = None,
    ) -> bytes:
        record = self._request(
            "read", path, timeout_seconds=timeout_seconds, cancel_event=cancel_event,
        )
        try:
            payload = base64.b64decode(record["data"], validate=True)
        except (KeyError, ValueError, TypeError) as exc:
            raise SandboxUnavailable("Invalid guest file encoding") from exc
        if len(payload) > MAX_FILE_BYTES:
            raise SandboxUnavailable("Guest file result exceeded transfer budget")
        return payload

    def write_file(
        self, path: str, data: bytes, *, timeout_seconds: float = 30,
        cancel_event: Event | None = None,
    ) -> int:
        if not isinstance(data, bytes) or len(data) > MAX_FILE_BYTES:
            raise ValueError("Guest write must be bytes of at most 64 KiB")
        record = self._request(
            "write", path, base64.b64encode(data).decode("ascii"),
            timeout_seconds=timeout_seconds, cancel_event=cancel_event,
        )
        if type(record.get("written")) is not int or record["written"] != len(data):
            raise SandboxUnavailable("Guest write acknowledgement is invalid")
        return len(data)

    def list_dir(
        self, path: str = ".", *, timeout_seconds: float = 30,
        cancel_event: Event | None = None,
    ) -> list[str]:
        if path == ".":
            path = "/workspace"
        record = self._request(
            "list", path, timeout_seconds=timeout_seconds, cancel_event=cancel_event,
        )
        names = record.get("entries")
        if (not isinstance(names, list) or len(names) > MAX_DIR_ENTRIES
                or any(not isinstance(x, str) or x in (".", "..")
                       or len(x.encode("utf-8")) > 255 for x in names)):
            raise SandboxUnavailable("Invalid guest directory listing")
        return names
