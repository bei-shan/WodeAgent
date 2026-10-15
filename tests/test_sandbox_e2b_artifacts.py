"""Host publication is gated by trusted callback and race-safe no-clobber."""
from __future__ import annotations

import hashlib
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from core.sandbox.contracts import SandboxUnavailable
from core.sandbox.e2b_artifacts import publish_e2b_artifact


class MemoryRemote:
    def __init__(self, data=b"guest artifact\x00\xff"):
        self.data = data
        self.calls = []

    def read_file(self, path, *, timeout_seconds=30):
        self.calls.append((path, timeout_seconds))
        return self.data


def dirs(tmp_path):
    (tmp_path / "artifacts").mkdir()
    return tmp_path


def approve_all(*args):
    return True


def test_approved_guest_bytes_published_only_under_host_artifacts(tmp_path):
    root = dirs(tmp_path)
    remote = MemoryRemote()
    approvals = []
    def authorize(path, destination, size, digest):
        approvals.append((path, destination, size, digest))
        return True
    result = publish_e2b_artifact(
        workspace=root, reader=remote, remote_path="file.bin",
        destination="artifacts/output.bin", approve=authorize,
    )
    assert (root / "artifacts/output.bin").read_bytes() == remote.data
    assert (root / "artifacts/output.bin").stat().st_mode & 0o777 == 0o600
    assert result.relative_path == "artifacts/output.bin"
    assert result.sha256 == hashlib.sha256(remote.data).hexdigest()
    assert result.bytes_written == len(remote.data)
    assert approvals == [("file.bin", "artifacts/output.bin",
                          len(remote.data), result.sha256)]
    assert len(remote.calls) == 1


def test_missing_or_negative_approval_refuses_host_write(tmp_path):
    root = dirs(tmp_path)
    reader = MemoryRemote()
    with pytest.raises(PermissionError, match="approval"):
        publish_e2b_artifact(
            workspace=root, reader=reader, remote_path="file",
            destination="artifacts/file", approve=None,
        )
    assert not reader.calls
    with pytest.raises(PermissionError, match="not explicitly approved"):
        publish_e2b_artifact(
            workspace=root, reader=reader, remote_path="file",
            destination="artifacts/file", approve=lambda *a: False,
        )
    assert not (root / "artifacts/file").exists()


@pytest.mark.parametrize("target", [
    "../outside", "/tmp/no", ".git/config", "foo", "artifacts/../outside",
    "artifacts/.env", "artifacts//file", "artifacts/sub/./file",
    "artifacts/sub/../file", "artifacts\\file", "artifacts/\x00oops",
])
def test_out_of_policy_destination_refused_before_cloud_read(tmp_path, target):
    reader = MemoryRemote()
    with pytest.raises(PermissionError):
        publish_e2b_artifact(
            workspace=dirs(tmp_path), reader=reader, remote_path="result.bin",
            destination=target, approve=approve_all,
        )
    assert not reader.calls


def test_file_symlink_target_not_overwritten(tmp_path):
    root = dirs(tmp_path)
    outside = tmp_path / "outside.txt"
    outside.write_text("protected")
    (root / "artifacts/link").symlink_to(outside)
    with pytest.raises(SandboxUnavailable):
        publish_e2b_artifact(
            workspace=root, reader=MemoryRemote(b"hijack"), remote_path="payload",
            destination="artifacts/link", approve=approve_all,
        )
    assert outside.read_text() == "protected"
    assert (root / "artifacts/link").is_symlink()
    assert sorted(p.name for p in (root / "artifacts").iterdir()) == ["link"]


def test_directory_symlink_pivot_refused(tmp_path):
    root = dirs(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / "artifacts/sub").symlink_to(outside, target_is_directory=True)
    with pytest.raises(SandboxUnavailable):
        publish_e2b_artifact(
            workspace=root, reader=MemoryRemote(b"hijack"), remote_path="payload",
            destination="artifacts/sub/file", approve=approve_all,
        )
    assert not (outside / "file").exists()


def test_no_existing_artifact_ever_overwritten(tmp_path):
    root = dirs(tmp_path)
    (root / "artifacts/result.txt").write_bytes(b"old")
    with pytest.raises(SandboxUnavailable):
        publish_e2b_artifact(
            workspace=root, reader=MemoryRemote(b"new"), remote_path="payload",
            destination="artifacts/result.txt", approve=approve_all,
        )
    assert (root / "artifacts/result.txt").read_bytes() == b"old"


def test_no_directory_creation_or_arbitrary_root_file_write(tmp_path):
    root = dirs(tmp_path)
    with pytest.raises(SandboxUnavailable):
        publish_e2b_artifact(
            workspace=root, reader=MemoryRemote(), remote_path="payload",
            destination="artifacts/not-existing/sub/result.txt",
            approve=approve_all,
        )
    assert sorted(p.name for p in (root / "artifacts").iterdir()) == []


def test_large_or_invalid_guest_data_cannot_be_published(tmp_path):
    root = dirs(tmp_path)
    for data in ("text-not-bytes", b"x" * (24576 + 1)):
        with pytest.raises(SandboxUnavailable):
            publish_e2b_artifact(
                workspace=root, reader=MemoryRemote(data), remote_path="payload",
                destination="artifacts/result", approve=approve_all,
            )
    assert not (root / "artifacts/result").exists()


def test_concurrent_publishers_cannot_clobber_each_other(tmp_path):
    root = dirs(tmp_path)
    def writer(i):
        try:
            publish_e2b_artifact(
                workspace=root, reader=MemoryRemote(str(i).encode()),
                remote_path="payload", destination="artifacts/race",
                approve=approve_all,
            )
            return True
        except SandboxUnavailable:
            return False
    with ThreadPoolExecutor(max_workers=8) as pool:
        outcomes = list(pool.map(writer, range(8)))
    assert outcomes.count(True) == 1
    assert (root / "artifacts/race").read_bytes() in [
        str(i).encode() for i in range(8)
    ]
    assert [p.name for p in (root / "artifacts").iterdir()] == ["race"]
