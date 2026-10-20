"""Pre-scan quarantine stays outside Agent workspace until vetted publication."""
from __future__ import annotations

import os
import secrets
import time
from io import BytesIO
from unittest.mock import patch

import pytest

from desktop.service.application.attachment_scanner import UploadRejected
from desktop.service.application.errors import ForbiddenError, PayloadTooLargeError
from tests.test_workspace_service import client


@pytest.mark.skipif(os.name != "posix", reason="Descriptor-anchored quarantine requires POSIX")
def test_clean_upload_is_not_visible_to_agent_while_scanning(client):
    app, http = client
    sid = http.post("/api/sessions", json={}).json()["id"]
    service = app.state.workspace_service
    workspace_file = service.workspace_base / sid / "private.txt"
    scanned = []

    def fake_scan(fd):
        scanned.append(os.read(fd, 100))
        assert not workspace_file.exists(), "Unscanned bytes leaked into workspace"
        quarantine = service.quarantine_base / sid
        assert len(list(quarantine.glob(".ridge-incoming-*"))) == 1

    with patch(
        "desktop.service.application.attachment_scanner.scan_uploaded_fd", fake_scan
    ):
        info = service.upload(sid, "private.txt", BytesIO(b"already scanned"))
    assert scanned == [b"already scanned"]
    assert workspace_file.read_bytes() == b"already scanned"
    assert info["attachment_ref"]
    assert list((service.quarantine_base / sid).iterdir()) == []
    assert service.prepare_attachments(sid, [info["attachment_ref"]])[0]["kind"] == "text"


@pytest.mark.skipif(os.name != "posix", reason="Descriptor-anchored quarantine requires POSIX")
def test_failed_scan_leaves_nothing_in_workspace_or_quarantine(client):
    app, http = client
    sid = http.post("/api/sessions", json={}).json()["id"]
    service = app.state.workspace_service

    def infected(fd):
        assert os.read(fd, 100) == b"EICAR-FIXTURE"
        raise UploadRejected("virus")

    with patch(
        "desktop.service.application.attachment_scanner.scan_uploaded_fd", infected
    ):
        with pytest.raises(ForbiddenError):
            service.upload(sid, "bad.txt", BytesIO(b"EICAR-FIXTURE"))
    assert not (service.workspace_base / sid / "bad.txt").exists()
    assert list((service.quarantine_base / sid).iterdir()) == []
    assert service._uploads.stats("local", sid) == (0, 0)


def test_oversized_upload_does_not_leave_quarantine_stage(client):
    app, http = client
    sid = http.post("/api/sessions", json={}).json()["id"]
    service = app.state.workspace_service
    with pytest.raises(PayloadTooLargeError):
        service.upload(sid, "huge.dat", BytesIO(b"x" * (25 * 1024 * 1024 + 1)))
    assert list((service.quarantine_base / sid).iterdir()) == []
    assert not (service.workspace_base / sid / "huge.dat").exists()


@pytest.mark.skipif(os.name != "posix", reason="POSIX quarantine GC")
def test_restarted_gc_removes_orphan_stage_but_not_unknown_files(client):
    app, http = client
    sid = http.post("/api/sessions", json={}).json()["id"]
    service = app.state.workspace_service
    with service._quarantine_directory_fd(sid) as fd:
        staged = ".ridge-incoming-" + secrets.token_hex(12)
        file_fd = os.open(staged, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=fd)
        os.write(file_fd, b"abandoned upload")
        os.close(file_fd)
        os.utime(staged, (time.time() - 8000, time.time() - 8000), dir_fd=fd)
        os.mkdir("keep", dir_fd=fd)
    assert service.cleanup_orphan_quarantine(now=time.time()) == 1
    items = list((service.quarantine_base / sid).iterdir())
    assert len(items) == 1 and items[0].name == "keep"
