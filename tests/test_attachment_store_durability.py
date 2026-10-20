"""Durable upload-capability, concurrency, quota and TTL acceptance tests."""
from __future__ import annotations

import os
import secrets
import sqlite3
import stat
import time
from concurrent.futures import ThreadPoolExecutor
from io import BytesIO

import pytest

from desktop.service.application.attachment_store import (
    AttachmentGrantStore, AttachmentQuotaExceeded,
)
from desktop.service.application.workspace import WorkspaceService
from tests.test_workspace_service import client


SID = "0123456789ab"
OTHER_SID = "a123456789ab"
IDENTITY = (1, 2, 7, 42, 43)


def test_tokens_persist_across_storage_process_instances_without_plaintext(tmp_path):
    db = tmp_path / "assets.sqlite3"
    a = AttachmentGrantStore(db)
    token = secrets.token_urlsafe(32)
    a.issue(token, "tenant-a", SID, "file.txt", IDENTITY, now=100)
    b = AttachmentGrantStore(db)
    assert b.resolve(token, "tenant-a", SID, now=110).identity == IDENTITY
    assert b.resolve(token, "tenant-b", SID, now=110) is None
    assert b.resolve(token, "tenant-a", OTHER_SID, now=110) is None
    assert b.resolve(token, "tenant-a", SID, now=7301) is None
    assert token not in db.read_bytes().decode("latin-1")
    if os.name == "posix":
        assert stat.S_IMODE(db.stat().st_mode) == 0o600


def test_sqlite_upload_admission_serialized_across_processlike_instances(tmp_path):
    path = tmp_path / "assets.sqlite3"
    a, b = AttachmentGrantStore(path), AttachmentGrantStore(path)
    a.MAX_SESSION_UPLOADS = b.MAX_SESSION_UPLOADS = 3

    def attempt(index):
        chosen = a if index % 2 else b
        try:
            chosen.issue(secrets.token_urlsafe(32), "tenant", SID,
                         f"f{index}.txt", IDENTITY)
            return True
        except AttachmentQuotaExceeded:
            return False

    with ThreadPoolExecutor(max_workers=10) as pool:
        outcomes = list(pool.map(attempt, range(35)))
    assert sum(outcomes) == 3
    assert AttachmentGrantStore(path).stats("tenant", SID) == (3, 21)


def test_size_quota_and_cross_tenant_isolation(tmp_path):
    store = AttachmentGrantStore(tmp_path / "assets.sqlite3")
    store.MAX_SESSION_BYTES = 12
    one = secrets.token_urlsafe(32)
    store.issue(one, "tenant-a", SID, "file.txt", IDENTITY)
    with pytest.raises(AttachmentQuotaExceeded):
        store.issue(secrets.token_urlsafe(32), "tenant-a", SID,
                    "large.txt", IDENTITY)
    store.issue(secrets.token_urlsafe(32), "tenant-b", SID,
                "file.txt", IDENTITY)
    assert store.stats("tenant-a", SID) == (1, 7)
    assert store.stats("tenant-b", SID) == (1, 7)
    store.revoke_session("tenant-a", SID)
    assert store.stats("tenant-a", SID) == (0, 0)
    assert store.stats("tenant-b", SID) == (1, 7)


def test_expiry_lease_removes_once_and_only_after_expiration(tmp_path):
    a = AttachmentGrantStore(tmp_path / "assets.sqlite3")
    token = secrets.token_urlsafe(32)
    a.issue(token, "tenant", SID, "file.txt", IDENTITY, now=100)
    assert a.expire(now=200) == []
    expired = a.expire(now=7500)
    assert len(expired) == 1
    assert expired[0].identity == IDENTITY
    assert a.expire(now=7500) == []


def test_web_upload_survives_reinstantiation_and_expires_with_file_cleanup(client):
    app, http = client
    sid = http.post("/api/sessions", json={}).json()["id"]
    uploaded = http.post(
        f"/api/sessions/{sid}/upload",
        files={"file": ("resume.txt", b"restart-safe", "text/plain")},
    ).json()
    reopened = WorkspaceService(app.state.controller, app.state.data_dir)
    parts = reopened.prepare_attachments(sid, [uploaded["attachment_ref"]])
    assert parts[0]["content"]["text"].find("restart-safe") != -1
    root = reopened.workspace_base / sid
    file = root / uploaded["path"]
    assert file.exists()
    result = reopened.cleanup_expired_uploads(now=time.time() + 7201)
    assert result["expired_grants"] >= 1
    assert result["files_removed"] >= 1
    assert not file.exists()
    assert reopened._uploads.resolve(uploaded["attachment_ref"], "local", sid) is None


def test_gc_preserves_modified_file_even_when_ticket_expires(client):
    app, http = client
    sid = http.post("/api/sessions", json={}).json()["id"]
    uploaded = http.post(
        f"/api/sessions/{sid}/upload",
        files={"file": ("edited.txt", b"initial", "text/plain")},
    ).json()
    file = app.state.workspace_service.workspace_base / sid / uploaded["path"]
    file.write_text("agent deliberately edited this file")
    result = app.state.workspace_service.cleanup_expired_uploads(now=time.time() + 7201)
    assert result["expired_grants"] >= 1
    assert file.read_text() == "agent deliberately edited this file"
