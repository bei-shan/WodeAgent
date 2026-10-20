"""Workspace File Service regressions: security, streaming and Web contract."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from desktop.service.app import create_app
from desktop.service.application import WorkspaceService
from desktop.service.application.errors import PayloadTooLargeError, ForbiddenError
from tests.test_web_service import FakeAgent


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("WEB_AUTH_TOKEN", raising=False)
    monkeypatch.setenv("MYCODEAGENT_DATA_DIR", str(tmp_path / ".mycodeagent"))
    app = create_app(agent_factory=FakeAgent, project_root=str(tmp_path))
    with TestClient(app) as http:
        yield app, http


def test_upload_duplicate_listing_preview_and_download(client):
    _, http = client
    sid = http.post("/api/sessions", json={}).json()["id"]
    first = http.post(
        f"/api/sessions/{sid}/upload",
        files={"file": ("code.py", b"first\nsecond\nthird\n", "text/plain")},
    )
    assert first.status_code == 200
    assert first.json()["path"] == "code.py"
    assert first.json()["name"] == "code.py"
    assert first.json()["size"] == 19
    assert len(first.json()["attachment_ref"]) >= 32
    second = http.post(
        f"/api/sessions/{sid}/upload",
        files={"file": ("code.py", b"other", "text/plain")},
    )
    assert second.status_code == 200
    assert second.json()["path"] == "code_1.py"
    listing = http.get(f"/api/sessions/{sid}/files", params={"path": ".", "limit": 1}).json()
    assert listing["truncated"] is True
    assert listing["entries"][0]["name"] == "code.py"
    text = http.get(
        f"/api/sessions/{sid}/files/content",
        params={"path": "code.py", "start_line": 2, "limit": 1},
    )
    assert text.status_code == 200
    assert text.json()["content"] == "2\tsecond"
    assert text.json()["truncated"] is True
    result = http.get(f"/api/sessions/{sid}/files/download", params={"path": "code.py"})
    assert result.status_code == 200
    assert result.content == b"first\nsecond\nthird\n"
    assert "attachment" in result.headers["content-disposition"]


def test_traversal_and_symlink_escape_rejected(client, tmp_path):
    _, http = client
    sid = http.post("/api/sessions", json={}).json()["id"]
    base = tmp_path / ".mycodeagent" / "sessions" / sid
    base.mkdir(parents=True, exist_ok=True)
    secret = tmp_path / "secret.txt"
    secret.write_text("PRIVATE")
    (base / "leak.txt").symlink_to(secret)
    (base / "outside-dir").symlink_to(tmp_path, target_is_directory=True)
    for path in ("../../../secret.txt", "leak.txt"):
        assert http.get(f"/api/sessions/{sid}/files/content", params={"path": path}).status_code == 403
        assert http.get(f"/api/sessions/{sid}/files/download", params={"path": path}).status_code == 403
    assert http.get(
        f"/api/sessions/{sid}/files",
        params={"path": "outside-dir"},
    ).status_code == 403
    listed = http.get(f"/api/sessions/{sid}/files").json()["entries"]
    assert not any(e["name"] in {"leak.txt", "outside-dir"} for e in listed)
    assert secret.read_text() == "PRIVATE"


def test_upload_sanitized_no_overwrite_and_size_cap(client, tmp_path):
    app, http = client
    sid = http.post("/api/sessions", json={}).json()["id"]
    result = http.post(
        f"/api/sessions/{sid}/upload",
        files={"file": ("../evil.txt", b"safe", "text/plain")},
    )
    assert result.status_code == 200
    assert result.json()["path"] == "evil.txt"
    root = tmp_path / ".mycodeagent" / "sessions" / sid
    assert (root / "evil.txt").read_bytes() == b"safe"
    assert not (tmp_path / "evil.txt").exists()
    service: WorkspaceService = app.state.workspace_service
    with pytest.raises(PayloadTooLargeError):
        service.upload(sid, "oversize.bin", BytesIO(b"x" * (25 * 1024 * 1024 + 1)))
    assert not (root / "oversize.bin").exists()


def test_missing_sessions_directory_and_binary_preview(client):
    _, http = client
    missing = "ffffffffffff"
    assert http.get(f"/api/sessions/{missing}/files").status_code == 404
    assert http.get(f"/api/sessions/{missing}/files/content", params={"path": "x"}).status_code == 404
    assert http.get(f"/api/sessions/{missing}/files/download", params={"path": "x"}).status_code == 404
    assert http.post(
        f"/api/sessions/{missing}/upload",
        files={"file": ("x.txt", b"secret", "text/plain")},
    ).status_code == 404
    sid = http.post("/api/sessions", json={}).json()["id"]
    assert http.get(f"/api/sessions/{sid}/files/content", params={"path": "."}).status_code == 400
    binary = http.post(
        f"/api/sessions/{sid}/upload",
        files={"file": ("binary.bin", b"\xff\x00\x80", "application/octet-stream")},
    )
    assert binary.status_code == 200
    assert http.get(
        f"/api/sessions/{sid}/files/content", params={"path": "binary.bin"},
    ).status_code == 400
    assert http.get(
        f"/api/sessions/{sid}/files/download", params={"path": "absent.txt"},
    ).status_code == 404


def test_concurrent_uploads_get_distinct_names(client):
    app, http = client
    sid = http.post("/api/sessions", json={}).json()["id"]
    service: WorkspaceService = app.state.workspace_service
    with ThreadPoolExecutor(max_workers=8) as pool:
        outputs = list(pool.map(
            lambda i: service.upload(sid, "shared.txt", BytesIO(str(i).encode())),
            range(8),
        ))
    assert len({result["path"] for result in outputs}) == 8


def test_service_has_no_frontend_or_fastapi_dependencies():
    root = Path(__file__).resolve().parents[1]
    service = (root / "desktop/service/application/workspace.py").read_text()
    app = (root / "desktop/service/app.py").read_text()
    assert "from fastapi" not in service
    assert "from starlette" not in service
    assert "def _session_root(" not in app
    assert "(root / path).resolve()" not in app


def test_nested_descriptor_paths_and_directory_symlink_swap(client, tmp_path, monkeypatch):
    """A replaced intermediate directory must not redirect a read."""
    import os
    app, http = client
    sid = http.post("/api/sessions", json={}).json()["id"]
    service = app.state.workspace_service
    root = tmp_path / ".mycodeagent" / "sessions" / sid
    nested = root / "child"
    nested.mkdir(parents=True)
    (nested / "payload.txt").write_text("safe")
    external = tmp_path / "outside"
    external.mkdir()
    (external / "payload.txt").write_text("EXTERNAL SECRET")
    assert service.content(sid, "child/payload.txt")["content"] == "1\tsafe"
    assert http.get(
        f"/api/sessions/{sid}/files/download",
        params={"path": "child/payload.txt"},
    ).content == b"safe"

    real_open = os.open
    swapped = False
    def changing_open(path, flags, *args, **kwargs):
        nonlocal swapped
        if path == "child" and "dir_fd" in kwargs and not swapped:
            swapped = True
            nested.rename(root / "child-original")
            nested.symlink_to(external, target_is_directory=True)
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", changing_open)
    try:
        with pytest.raises(ForbiddenError):
            service.content(sid, "child/payload.txt")
    finally:
        monkeypatch.setattr(os, "open", real_open)
    assert swapped and (external / "payload.txt").read_text() == "EXTERNAL SECRET"


def test_session_root_symlink_rejected_and_upload_not_escaped(client, tmp_path):
    app, http = client
    sid = http.post("/api/sessions", json={}).json()["id"]
    root = tmp_path / ".mycodeagent" / "sessions" / sid
    app.state.workspace_service.list(sid)  # Materialize the session root first.
    other = tmp_path / "other"
    other.mkdir()
    root.rename(root.with_name(root.name + "-saved"))
    root.symlink_to(other, target_is_directory=True)
    with pytest.raises(ForbiddenError):
        app.state.workspace_service.upload(sid, "test.txt", BytesIO(b"hello"))
    assert not (other / "test.txt").exists()
