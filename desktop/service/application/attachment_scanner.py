"""Optional local ClamAV scan gate using clamd's Unix INSTREAM protocol.

For untrusted deployments, RIDGE_ATTACHMENT_SCAN_REQUIRED=1 is fail-closed.
All uploads are scanned before issuing a model attachment credential; local
desktop installations without this flag keep their existing trusted mode.
Only an operator-configured Unix socket is opened, never arbitrary URLs.
"""
from __future__ import annotations

import os
import socket
import struct


class UploadRejected(ValueError):
    """Upload failed a required malware scan or the scanner is unavailable."""


def scan_uploaded_file(file_path: str | os.PathLike[str]) -> None:
    required = os.getenv("RIDGE_ATTACHMENT_SCAN_REQUIRED", "").strip().lower() in {
        "1", "true", "yes", "on",
    }
    sock_path = os.getenv("RIDGE_CLAMD_UNIX_SOCKET", "").strip()
    if not sock_path:
        if required:
            raise UploadRejected("Required attachment scanner is unavailable")
        return
    if os.name != "posix" or not sock_path.startswith("/") or len(sock_path) > 512:
        raise UploadRejected("Invalid ClamAV Unix socket configuration")
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as stream:
            stream.settimeout(8)
            stream.connect(sock_path)
            stream.sendall(b"zINSTREAM\x00")
            with open(file_path, "rb") as source:
                while True:
                    chunk = source.read(64 * 1024)
                    if not chunk:
                        break
                    stream.sendall(struct.pack("!I", len(chunk)))
                    stream.sendall(chunk)
            stream.sendall(struct.pack("!I", 0))
            reply = bytearray()
            while len(reply) < 2048:
                received = stream.recv(min(2048 - len(reply), 512))
                if not received:
                    break
                reply.extend(received)
                if b"\x00" in reply:
                    break
    except (OSError, TimeoutError) as exc:
        raise UploadRejected("Attachment malware scanner unavailable") from exc
    # The exact ClamAV verdict must be clean; any FOUND, ERROR, truncated
    # reply or unexpected protocol is a rejection.
    if not reply.endswith(b"OK\x00") or b"FOUND" in reply or b"ERROR" in reply:
        raise UploadRejected("Attachment did not pass malware scan")
