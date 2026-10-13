"""Portable tree archive HTTP integration and hostile input rejection."""
from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient

from core.context_engine.history_manager import HistoryManager
from core.session_manager import SessionManager
from core.session_store import build_session_snapshot
from desktop.service.app import create_app
from desktop.service.application.session_portability import FORMAT, MAX_BYTES, VERSION
from tests.test_web_service import FakeAgent


def sample_archive():
    return {
        "format": FORMAT, "version": VERSION,
        "title": "Portable Demo", "cursor_id": "a1b2c3d4",
        "messages": [
            {"id": "a1b2c3d4", "parent_id": None, "role": "user", "content": "hi"},
            {"id": "b1b2c3d4", "parent_id": "a1b2c3d4", "role": "assistant", "content": "old reply"},
            {"id": "c1b2c3d4", "parent_id": "a1b2c3d4", "role": "assistant", "content": "other branch"},
        ],
        "labels": {"a1b2c3d4": "checkpoint"},
    }


def app_for(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("WEB_AUTH_TOKEN", raising=False)
    return create_app(agent_factory=FakeAgent, project_root=str(tmp_path))


def test_export_never_leaks_internal_fields_and_import_creates_new_id(tmp_path, monkeypatch):
    with TestClient(app_for(tmp_path, monkeypatch)) as client:
        sid = client.post("/api/sessions", json={"title": "Private Example"}).json()["id"]
        hm = HistoryManager()
        first = hm.append_user("hello")
        hm.append_assistant("SECRET SIBLING CHAT")
        hm.fork(first.message_id)
        selected = hm.append_assistant("selected")
        hm.append_label(first.message_id, "Milestone")
        snapshot = build_session_snapshot(
            system_messages=[{"role": "system", "content": "SECRET-SYSTEM-KEY"}],
            history_messages=hm.serialize_messages(), tool_schema=[],
            project_root=str(tmp_path), schema_version=2,
            history_entries=hm.serialize_entries(), cursor_id=selected.message_id,
            labels=hm._labels, read_cache={"token": "SECRET-TOOL-CACHE"},
            teams_snapshot={"secret": "SECRET-TEAMS-STATE"},
        )
        SessionManager().save_session(sid, snapshot)
        response = client.get(f"/api/sessions/{sid}/export")
        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-store"
        body = response.json()
        assert set(body) == {"format", "version", "title", "cursor_id", "messages", "labels"}
        assert "SECRET-SYSTEM-KEY" not in response.text
        assert "SECRET-TOOL-CACHE" not in response.text
        assert "SECRET-TEAMS-STATE" not in response.text
        assert body["cursor_id"] == selected.message_id
        assert body["labels"][first.message_id] == "Milestone"
        assert any(m["content"] == "SECRET SIBLING CHAT" for m in body["messages"])
        assert all(set(m) == {"id", "parent_id", "role", "content"} for m in body["messages"])

        imported = client.post("/api/sessions/import", json=body)
        assert imported.status_code == 201
        clone = imported.json()["id"]
        assert clone != sid
        new_snapshot = SessionManager().load_session(clone)
        assert new_snapshot["system_messages"] == []
        assert new_snapshot["read_cache"] == {}
        assert new_snapshot["teams_snapshot"] == {}
        assert new_snapshot["cursor_id"] == selected.message_id
        assert client.get(f"/api/sessions/{clone}/export").json() == body
        assert client.get(f"/api/sessions/{clone}/history").json()["messages"] == [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "selected"},
        ]

    # The newly imported ID is also usable by a restarted process.
    with TestClient(app_for(tmp_path, monkeypatch)) as client:
        tree = client.get(f"/api/sessions/{clone}/tree")
        assert tree.status_code == 200
        assert tree.json()["cursor_id"] == selected.message_id
        assert client.post(f"/api/sessions/{clone}/fork", json={
            "target_id": first.message_id,
        }).status_code == 200


def test_import_rejects_untrusted_graph_and_never_creates_session(tmp_path, monkeypatch):
    with TestClient(app_for(tmp_path, monkeypatch)) as client:
        before = set(i.id for i in SessionManager().list_sessions())
        good = sample_archive()
        invalid = []
        for change in [
            {"version": 2},
            {"format": "unsafe"},
            {"cursor_id": "00000000"},
            {"labels": {"00000000": "bad"}},
            {"system_messages": [{"role": "system", "content": "steal"}]},
            {"messages": good["messages"] + [dict(good["messages"][0])]},
            {"messages": [{"id": "a1b2c3d4", "parent_id": "b1b2c3d4",
                            "role": "user", "content": "cycle"},
                           {"id": "b1b2c3d4", "parent_id": "a1b2c3d4",
                            "role": "assistant", "content": "cycle"}]},
            {"messages": [{"id": "a1b2c3d4", "parent_id": None,
                            "role": "tool", "content": "forbidden"}]},
            {"messages": [{"id": "a1b2c3d4", "parent_id": None,
                            "role": ["user"], "content": "bad"}]},
            {"messages": [{"id": "a1b2c3d4", "parent_id": None,
                            "role": "user", "content": "X" * 4097}]},
            {"cursor_id": None},
        ]:
            bad = dict(good)
            bad.update(change)
            invalid.append(bad)
        for payload in invalid:
            response = client.post("/api/sessions/import", json=payload)
            assert response.status_code in (400, 413), (response.status_code, payload.keys())
        assert set(i.id for i in SessionManager().list_sessions()) == before
        raw_duplicate = b'{"format":"ridgecode.portable-session","format":"x"}'
        assert client.post("/api/sessions/import", content=raw_duplicate,
                           headers={"content-type": "application/json"}).status_code == 400
        too_large = b" " * (MAX_BYTES + 1)
        assert client.post("/api/sessions/import", content=too_large,
                           headers={"content-type": "application/json"}).status_code == 413
        assert client.post("/api/sessions/import", content=b"{}",
                           headers={"content-type": "text/plain"}).status_code == 400
        assert set(i.id for i in SessionManager().list_sessions()) == before


def test_export_empty_legacy_session_and_busy_or_unknown_guards(tmp_path, monkeypatch):
    with TestClient(app_for(tmp_path, monkeypatch)) as client:
        sid = client.post("/api/sessions", json={"title": "Blank"}).json()["id"]
        data = client.get(f"/api/sessions/{sid}/export").json()
        assert data["messages"] == [] and data["cursor_id"] is None
        live = client.app.state.controller.get_session(sid)
        live._busy = True
        try:
            assert client.get(f"/api/sessions/{sid}/export").status_code == 409
        finally:
            live._busy = False
        assert client.get("/api/sessions/ffffffffffff/export").status_code == 404


def test_portable_service_does_not_depend_on_http_or_terminal():
    import ast
    path = Path(__file__).resolve().parents[1] / "desktop/service/application/session_portability.py"
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.ImportFrom):
            assert not (node.module or "").startswith(("fastapi", "tui", "desktop.web"))
