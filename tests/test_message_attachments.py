"""Attachments are session-confined and media bytes remain transient."""
from __future__ import annotations

import base64
import json
from io import BytesIO

import pytest
from fastapi.testclient import TestClient

from core.runtime.attachment_context import (
    apply_transient_attachments, make_attachment_part
)
from desktop.service.app import create_app
from desktop.service.application.errors import InvalidInputError, ForbiddenError
from tests.test_web_service import FakeAgent


def _png_bytes() -> bytes:
    from PIL import Image
    stream = BytesIO()
    Image.new("RGB", (2, 2), (0, 100, 200)).save(stream, format="PNG")
    return stream.getvalue()


PNG = _png_bytes()


def test_transient_vision_input_not_mutated_into_saved_messages():
    part = make_attachment_part("shot.png", PNG)
    history = [{"role": "system", "content": "be safe"},
               {"role": "user", "content": "Please describe image"}]
    for_model = apply_transient_attachments(history, [part], first_step=True)
    assert history[1]["content"] == "Please describe image"
    blocks = for_model[1]["content"]
    assert blocks[0]["text"] == "Please describe image"
    assert blocks[1]["type"] == "image_url"
    url = blocks[1]["image_url"]["url"]
    assert url.startswith("data:image/png;base64,")
    assert base64.b64decode(url.split(",", 1)[1]) == PNG
    assert apply_transient_attachments(history, [part], first_step=False) is history


def test_media_sniff_ignores_misleading_extension_and_rejects_large_input():
    assert make_attachment_part("fake.txt", PNG)["kind"] == "image"
    assert make_attachment_part("README.md", b"Hello")["kind"] == "text"
    from pypdf import PdfWriter
    pdf = PdfWriter()
    pdf.add_blank_page(width=72, height=72)
    stream = BytesIO()
    pdf.write(stream)
    assert make_attachment_part("report.pdf", stream.getvalue())["content"] is None
    with pytest.raises(ValueError, match="4 MiB"):
        make_attachment_part("huge.png", PNG + b"z" * (4 * 1024 * 1024))


def test_attachment_input_rejects_binary_disguised_as_text():
    with pytest.raises(ValueError, match="Binary"):
        make_attachment_part("x.txt", b"secret\x00binary")


@pytest.fixture
def web(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("WEB_AUTH_TOKEN", raising=False)
    monkeypatch.setenv("MYCODEAGENT_DATA_DIR", str(tmp_path / ".mycodeagent"))
    app = create_app(agent_factory=FakeAgent, project_root=str(tmp_path))
    with TestClient(app) as client:
        yield app, client


def test_web_uploaded_image_attached_to_one_turn_without_base64_history(web):
    app, client = web
    sid = client.post("/api/sessions", json={}).json()["id"]
    response = client.post(
        f"/api/sessions/{sid}/upload",
        files={"file": ("photo.png", PNG, "text/plain")},
    )
    assert response.status_code == 200
    uploaded_path = response.json()["path"]
    upload_ref = response.json()["attachment_ref"]
    result = client.post(
        f"/api/sessions/{sid}/messages",
        json={"content": "", "attachments": [upload_ref]},
    )
    assert result.status_code == 200
    from time import sleep
    for _ in range(30):
        if not app.state.controller.get_session(sid).busy:
            break
        sleep(.04)
    agent = app.state.controller.get_session(sid).ensure_agent()
    assert agent._turn_attachment_parts == []
    hist = client.get(f"/api/sessions/{sid}/history").json()
    serialized = json.dumps(hist)
    assert "photo.png" in serialized
    assert "data:image/png" not in serialized
    assert "FAKE_PNG_CONTENT" not in serialized


def test_attachment_cross_session_and_traversal_denied(web):
    app, client = web
    sid_a = client.post("/api/sessions", json={}).json()["id"]
    sid_b = client.post("/api/sessions", json={}).json()["id"]
    upload = client.post(
        f"/api/sessions/{sid_a}/upload",
        files={"file": ("only-a.txt", b"SECRET_A", "text/plain")},
    ).json()
    bad = client.post(
        f"/api/sessions/{sid_b}/messages",
        json={"content": "hi", "attachments": [upload["attachment_ref"]]},
    )
    assert bad.status_code in (400, 403, 404)
    traversal = client.post(
        f"/api/sessions/{sid_b}/messages",
        json={"content": "hi", "attachments": ["../" + sid_a + "/only-a.txt"]},
    )
    assert traversal.status_code in (400, 403)
    # Existing workspace files cannot become model attachments unless uploaded.
    assert client.post(
        f"/api/sessions/{sid_a}/messages",
        json={"content": "hi", "attachments": ["only-a.txt"]},
    ).status_code == 403
    assert client.post(
        f"/api/sessions/{sid_b}/messages",
        json={"content": "", "attachments": []},
    ).status_code == 400


def test_attachment_rejects_file_mutation_after_upload(web):
    app, client = web
    sid = client.post("/api/sessions", json={}).json()["id"]
    uploaded = client.post(
        f"/api/sessions/{sid}/upload",
        files={"file": ("notes.txt", b"safe", "text/plain")},
    ).json()
    # Direct workspace mutation by an Agent/tool must invalidate the ticket.
    workspace = app.state.workspace_service.workspace_base / sid
    (workspace / uploaded["path"]).write_text("changed by tool")
    result = client.post(
        f"/api/sessions/{sid}/messages",
        json={"content": "summarize", "attachments": [uploaded["attachment_ref"]]},
    )
    assert result.status_code == 403


def test_attachment_ticket_is_unique_and_does_not_publish_host_path(web):
    _, client = web
    sid = client.post("/api/sessions", json={}).json()["id"]
    first = client.post(
        f"/api/sessions/{sid}/upload",
        files={"file": ("a.txt", b"hello", "text/plain")},
    ).json()
    second = client.post(
        f"/api/sessions/{sid}/upload",
        files={"file": ("a.txt", b"hello", "text/plain")},
    ).json()
    assert first["attachment_ref"] != second["attachment_ref"]
    assert "/" not in first["attachment_ref"]
    assert first["path"] != second["path"]
