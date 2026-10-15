"""Bounded read-only E2B guest filesystem bridge (no implicit host export).

A remote E2B workspace has no host volume. All file access runs INSIDE the
guest and follows a dirfd/O_NOFOLLOW walk; bytes return only to the calling
application's memory. A separate, explicitly authorized artifact publication
workflow is required before writing anything into the host project.
"""
from __future__ import annotations

import base64
import json
import shlex
from threading import Event

from core.sandbox.contracts import SandboxCommandProvider, SandboxUnavailable

REMOTE_ROOT = "/home/user"
MAX_REMOTE_FILE_BYTES = 24 * 1024
MAX_REMOTE_ENTRIES = 64
MAX_REMOTE_RESPONSE = 40 * 1024

_GUEST_SCRIPT = r'''
import base64
import json
import os
import stat
import sys

ROOT = "/home/user"
FILE_LIMIT = 24576
ENTRIES_LIMIT = 64
FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW

def emit(result):
    print(json.dumps(result, ensure_ascii=True, separators=(",", ":")))

def run():
    op, path = sys.argv[1:3]
    if not (path == ROOT or path.startswith(ROOT + "/")):
        raise PermissionError("outside root")
    parts = path[len(ROOT):].strip("/").split("/")
    if parts == [""]:
        parts = []
    if (len(parts) > 32 or
            any(not p or p in (".", "..") or len(p.encode()) > 255 for p in parts)):
        raise PermissionError("invalid components")
    fd = os.open(ROOT, FLAGS)
    try:
        parents = parts if op == "list" else parts[:-1]
        for name in parents:
            next_fd = os.open(name, FLAGS, dir_fd=fd)
            os.close(fd)
            fd = next_fd
        if op == "read":
            if not parts:
                raise PermissionError("root not a regular file")
            handle = os.open(
                parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                dir_fd=fd,
            )
            try:
                info = os.fstat(handle)
                if not stat.S_ISREG(info.st_mode) or info.st_size > FILE_LIMIT:
                    raise ValueError("file too large or not regular")
                raw = os.read(handle, FILE_LIMIT + 1)
                if len(raw) > FILE_LIMIT:
                    raise ValueError("file exceeds response budget")
                emit({"ok": True, "size": len(raw),
                      "data": base64.b64encode(raw).decode("ascii")})
            finally:
                os.close(handle)
        elif op == "list":
            results = []
            with os.scandir(fd) as entries:
                for item in entries:
                    if len(results) == ENTRIES_LIMIT:
                        raise ValueError("too many entries")
                    results.append(item.name)
            emit({"ok": True, "entries": sorted(results)})
        else:
            raise ValueError("unsupported operation")
    finally:
        os.close(fd)

try:
    run()
except (OSError, ValueError, PermissionError):
    emit({"ok": False, "error": "ACCESS_DENIED_OR_LIMIT"})
'''


def _remote_path(path: str) -> str:
    if not isinstance(path, str) or not path or len(path) > 1024:
        raise ValueError("Remote path must be a bounded nonempty string")
    if ("\x00" in path or "\\" in path
            or any(ord(c) < 32 or ord(c) == 127 for c in path)):
        raise PermissionError("Remote path contains invalid characters")
    if path == "." or path == REMOTE_ROOT:
        return REMOTE_ROOT
    if path.startswith("/"):
        if not path.startswith(REMOTE_ROOT + "/"):
            raise PermissionError("Remote path outside user workspace")
        relative = path[len(REMOTE_ROOT) + 1:]
    else:
        relative = path
    parts = relative.split("/")
    if (len(parts) > 32 or
            any(not p or p in (".", "..") or len(p.encode("utf-8")) > 255
                for p in parts)):
        raise PermissionError("Remote path traversal or invalid component")
    return REMOTE_ROOT + "/" + "/".join(parts)


class E2BGuestFilesystem:
    """Read/list only; never maps a remote file path to a host destination."""

    def __init__(self, provider: SandboxCommandProvider):
        if provider.kind != "e2b":
            raise SandboxUnavailable("Remote guest filesystem requires E2B VM")
        self._provider = provider

    def _request(
        self, action: str, path: str, *,
        timeout_seconds: float = 30,
        cancel_event: Event | None = None,
    ) -> dict:
        target = _remote_path(path)
        command = "exec python3 -c " + " ".join(
            shlex.quote(x) for x in (_GUEST_SCRIPT, action, target)
        )
        result = self._provider.execute(
            command, directory=REMOTE_ROOT,
            timeout_seconds=timeout_seconds,
            cancel_event=cancel_event,
        )
        if result.interrupted:
            raise InterruptedError("Remote E2B file operation interrupted")
        if result.timed_out:
            raise TimeoutError("Remote E2B file operation timed out")
        if (result.exit_code != 0 or not isinstance(result.stdout, str)
                or len(result.stdout.encode("utf-8")) > MAX_REMOTE_RESPONSE):
            raise SandboxUnavailable("Remote E2B file command failed or exceeded budget")
        try:
            output = json.loads(result.stdout)
        except (ValueError, TypeError):
            raise SandboxUnavailable("Invalid E2B guest filesystem response") from None
        if not isinstance(output, dict):
            raise SandboxUnavailable("Invalid E2B guest filesystem response")
        if output.get("ok") is not True:
            raise PermissionError("Remote E2B file operation denied or exceeded limit")
        return output

    def read_file(
        self, path: str, *, timeout_seconds: float = 30,
        cancel_event: Event | None = None,
    ) -> bytes:
        output = self._request(
            "read", path, timeout_seconds=timeout_seconds,
            cancel_event=cancel_event,
        )
        try:
            data = base64.b64decode(output["data"], validate=True)
        except (ValueError, TypeError, KeyError):
            raise SandboxUnavailable("Invalid E2B file encoding") from None
        if (len(data) > MAX_REMOTE_FILE_BYTES
                or type(output.get("size")) is not int
                or output["size"] != len(data)):
            raise SandboxUnavailable("Remote E2B file transfer integrity failed")
        return data

    def list_dir(
        self, path: str = ".", *, timeout_seconds: float = 30,
        cancel_event: Event | None = None,
    ) -> list[str]:
        output = self._request(
            "list", path, timeout_seconds=timeout_seconds,
            cancel_event=cancel_event,
        )
        names = output.get("entries")
        if (not isinstance(names, list) or len(names) > MAX_REMOTE_ENTRIES
                or any(not isinstance(x, str) or not x or x in (".", "..")
                       or "/" in x or "\x00" in x
                       or len(x.encode("utf-8")) > 255 for x in names)):
            raise SandboxUnavailable("Invalid E2B directory listing")
        return names

    def write_file(self, path: str, data: bytes, **kwargs: object) -> int:
        raise SandboxUnavailable(
            "Remote E2B write and host artifact publication need explicit separate authorization"
        )
