"""ClamAV INSTREAM protocol and mandatory fail-closed upload acceptance."""
from __future__ import annotations

import os
import socket
import struct
import threading

import pytest

from tests.test_workspace_service import client


pytestmark = pytest.mark.skipif(os.name != "posix", reason="ClamAV Unix socket")


def _server(path: str, verdict: bytes, ready: threading.Event, received: list[bytes]):
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server:
        server.bind(path)
        server.listen(1)
        ready.set()
        server.settimeout(5)
        conn, _ = server.accept()
        with conn:
            conn.settimeout(5)
            def exact(n):
                data = bytearray()
                while len(data) < n:
                    piece = conn.recv(n - len(data))
                    if not piece:
                        raise RuntimeError("Unexpected EOF")
                    data.extend(piece)
                return bytes(data)
            assert exact(len(b"zINSTREAM\x00")) == b"zINSTREAM\x00"
            chunks = []
            while True:
                count = struct.unpack("!I", exact(4))[0]
                if count == 0:
                    break
                chunks.append(exact(count))
            received.append(b"".join(chunks))
            conn.sendall(verdict)


def test_clean_scanner_upload_grants_credential(client, tmp_path, monkeypatch):
    app, http = client
    sid = http.post("/api/sessions", json={}).json()["id"]
    sock_path = str(tmp_path / "clamd.sock")
    got = []
    ready = threading.Event()
    thread = threading.Thread(
        target=_server, args=(sock_path, b"stream: OK\x00", ready, got),
        daemon=True,
    )
    thread.start()
    assert ready.wait(3)
    monkeypatch.setenv("RIDGE_CLAMD_UNIX_SOCKET", sock_path)
    monkeypatch.setenv("RIDGE_ATTACHMENT_SCAN_REQUIRED", "1")
    response = http.post(
        f"/api/sessions/{sid}/upload",
        files={"file": ("clean.txt", b"clean upload payload", "text/plain")},
    )
    thread.join(3)
    assert response.status_code == 200, response.text
    assert got == [b"clean upload payload"]
    assert response.json()["attachment_ref"]
    assert app.state.workspace_service._uploads.stats("local", sid) == (1, 20)


def test_malware_verdict_rejects_and_unlinks_upload(client, tmp_path, monkeypatch):
    app, http = client
    sid = http.post("/api/sessions", json={}).json()["id"]
    sock_path = str(tmp_path / "clamd.sock")
    got = []
    ready = threading.Event()
    thread = threading.Thread(
        target=_server,
        args=(sock_path, b"stream: Test-Eicar-Signature FOUND\x00", ready, got),
        daemon=True,
    )
    thread.start()
    assert ready.wait(3)
    monkeypatch.setenv("RIDGE_CLAMD_UNIX_SOCKET", sock_path)
    monkeypatch.setenv("RIDGE_ATTACHMENT_SCAN_REQUIRED", "1")
    response = http.post(
        f"/api/sessions/{sid}/upload",
        files={"file": ("infected.txt", b"malicious candidate", "text/plain")},
    )
    thread.join(3)
    assert response.status_code == 403
    assert not (app.state.workspace_service.workspace_base / sid / "infected.txt").exists()
    assert app.state.workspace_service._uploads.stats("local", sid) == (0, 0)


def test_missing_required_scanner_does_not_publish_upload(client, monkeypatch):
    app, http = client
    sid = http.post("/api/sessions", json={}).json()["id"]
    monkeypatch.delenv("RIDGE_CLAMD_UNIX_SOCKET", raising=False)
    monkeypatch.setenv("RIDGE_ATTACHMENT_SCAN_REQUIRED", "true")
    response = http.post(
        f"/api/sessions/{sid}/upload",
        files={"file": ("blocked.txt", b"fail closed", "text/plain")},
    )
    assert response.status_code == 403
    assert not (app.state.workspace_service.workspace_base / sid / "blocked.txt").exists()
    assert app.state.workspace_service._uploads.stats("local", sid) == (0, 0)
