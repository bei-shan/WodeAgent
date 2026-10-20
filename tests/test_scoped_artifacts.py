"""Persistent private artifact ownership and path hardening."""
from __future__ import annotations

import os
import time
import pytest

from core.memory.contracts import MemoryScope
from core.sandbox.scoped_artifacts import ScopedArtifactStore, ArtifactAccessDenied


A = MemoryScope("tenant-one", "user", "project", "agent")
B = MemoryScope("tenant-two", "user", "project", "agent")


def test_artifacts_are_opaque_and_principal_session_scoped(tmp_path):
    store = ScopedArtifactStore(tmp_path / "private")
    entry = store.put(A, "session-one", b"image-data", media_type="image/png")
    assert len(entry.id) == 32
    assert store.read(A, "session-one", entry.id) == b"image-data"
    reopened = ScopedArtifactStore(tmp_path / "private")
    assert reopened.read(A, "session-one", entry.id) == b"image-data"
    for scope, session in [(B, "session-one"), (A, "session-other")]:
        with pytest.raises(ArtifactAccessDenied):
            reopened.read(scope, session, entry.id)
        with pytest.raises(ArtifactAccessDenied):
            reopened.delete(scope, session, entry.id)
    assert store.delete(A, "session-one", entry.id) is True
    with pytest.raises(ArtifactAccessDenied):
        reopened.read(A, "session-one", entry.id)


def test_rejects_traversal_wrong_mime_and_unverified_scope(tmp_path):
    store = ScopedArtifactStore(tmp_path / "art")
    entry = store.put(A, "x", b"ok")
    for key in ("../../etc/passwd", "/etc/passwd", entry.id + "a"):
        with pytest.raises(ArtifactAccessDenied):
            store.read(A, "x", key)
    for options in (
        {"media_type": "text/html"}, {"ttl_seconds": 0},
        {"ttl_seconds": 86401},
    ):
        with pytest.raises(ValueError):
            store.put(A, "x", b"abc", **options)
    with pytest.raises(ValueError):
        store.put(A, "x", b"a" * (2 * 1024 * 1024 + 1))
    with pytest.raises(ArtifactAccessDenied):
        store.put(A, "../x", b"abc")
    with pytest.raises(ArtifactAccessDenied):
        store.put({"tenant": "fake"}, "x", b"abc")


def test_symlink_blob_swap_fails_closed(tmp_path):
    store = ScopedArtifactStore(tmp_path / "art")
    blob = store.put(A, "s", b"private")
    target = tmp_path / "host-secret"
    target.write_bytes(b"never release")
    artifact_path = store.blobs / blob.id
    artifact_path.unlink()
    artifact_path.symlink_to(target)
    with pytest.raises((OSError, ArtifactAccessDenied)):
        store.read(A, "s", blob.id)
    assert target.read_bytes() == b"never release"


def test_ttl_expiry_is_enforced_without_cleanup(tmp_path, monkeypatch):
    now = int(time.time())
    monkeypatch.setattr("core.sandbox.scoped_artifacts.time.time", lambda: now)
    store = ScopedArtifactStore(tmp_path / "art")
    blob = store.put(A, "s", b"short", ttl_seconds=1)
    monkeypatch.setattr("core.sandbox.scoped_artifacts.time.time", lambda: now + 2)
    with pytest.raises(ArtifactAccessDenied):
        store.read(A, "s", blob.id)
    assert store.purge_expired() == 1


def test_storage_dir_is_private_and_rejects_symlink_root(tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(real, target_is_directory=True)
    with pytest.raises(ArtifactAccessDenied):
        ScopedArtifactStore(alias)
    store = ScopedArtifactStore(real)
    assert store.root.stat().st_mode & 0o777 == 0o700
    assert store.blobs.stat().st_mode & 0o777 == 0o700


def test_artifact_cap_per_session(tmp_path):
    store = ScopedArtifactStore(tmp_path / "art")
    for i in range(32):
        store.put(A, "s", b"data")
    with pytest.raises(ValueError, match="quota"):
        store.put(A, "s", b"again")
    assert store.put(A, "other", b"allowed")
