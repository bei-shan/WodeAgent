"""Authenticated read-only finite cursor/SSE replay contract at hosted API."""
from __future__ import annotations

from fastapi.testclient import TestClient

from desktop.service.hosted_app import create_hosted_app
from tests.test_hosted_identity_api import TestPrivateStore, TestVerifier, headers


class TestEvents:
    def __init__(self):
        self.calls = []
        self.records = {}

    def events(self, scope, sid, *, after=0, limit=100):
        key = (scope.tenant_id, scope.user_id, scope.project_id, sid)
        self.calls.append((key, after, limit))
        return [e for e in self.records.get(key, []) if e["id"] > after][:limit]


def _fixture():
    store, events = TestPrivateStore(), TestEvents()
    app = create_hosted_app(verifier=TestVerifier(), store=store, turn_events=events)
    return store, events, TestClient(app)


def test_event_replay_requires_verified_member_and_exact_session():
    store, events, client = _fixture()
    with client:
        session = client.post("/api/v2/sessions", headers=headers("alice"), json={}).json()["id"]
        key = ("t1", "u1", "p1", session)
        events.records[key] = [
            {"id": 1, "turn_id": "turn-a", "state": "queued", "fence": 0},
            {"id": 2, "turn_id": "turn-a", "state": "completed", "fence": 1},
        ]
        path = f"/api/v2/sessions/{session}/events"
        assert client.get(path).status_code == 401
        assert client.get(path, headers=headers("bob")).status_code == 404
        assert client.get(path, headers=headers("other-user")).status_code == 404
        assert len(client.get(path, headers=headers("alice")).json()) == 2
        assert client.get(path + "?after=1&limit=1", headers=headers("alice")).json() == [events.records[key][1]]
        assert client.get(path + "?limit=0", headers=headers("alice")).status_code == 422
        assert client.get(path + "?after=-1", headers=headers("alice")).status_code == 422
        assert client.get(path + "?limit=101", headers=headers("alice")).status_code == 422
        assert len(events.calls) == 2  # forbidden requests never queried event storage
        store.memberships.remove(("t1", "u1", "p1"))
        assert client.get(path, headers=headers("alice")).status_code == 403
        assert len(events.calls) == 2
        store.memberships.add(("t1", "u1", "p1"))
        assert client.delete(f"/api/v2/sessions/{session}", headers=headers("alice")).status_code == 204
        assert client.get(path, headers=headers("alice")).status_code == 404


def test_sse_resumes_from_last_event_id_and_uses_valid_framing():
    store, events, client = _fixture()
    with client:
        sid = client.post("/api/v2/sessions", headers=headers("alice"), json={}).json()["id"]
        events.records[("t1", "u1", "p1", sid)] = [
            {"id": 1, "turn_id": "one", "state": "queued", "fence": 0},
            {"id": 2, "turn_id": "one", "state": "running", "fence": 1},
            {"id": 3, "turn_id": "one", "state": "completed", "fence": 1},
        ]
        uri = f"/api/v2/sessions/{sid}/events/stream"
        resp = client.get(uri, headers={**headers("alice"), "Last-Event-ID": "1"})
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/event-stream")
        assert resp.headers["cache-control"] == "no-store"
        assert resp.text.startswith("id: 2\nevent: turn-state\ndata: ")
        assert "\n\nid: 3\nevent: turn-state\ndata: " in resp.text
        assert "id: 1\n" not in resp.text
        assert client.get(uri, headers={**headers("alice"), "Last-Event-ID": "3"}).text == ": no-new-events\n\n"
        for bad in ("-1", "abc", "99999999999999999999", "1\nattack"):
            assert client.get(uri, headers={**headers("alice"), "Last-Event-ID": bad}).status_code == 400
        assert client.get(uri + "?after=2", headers={**headers("alice"), "Last-Event-ID": "1"}).status_code == 400
        assert client.get(uri, headers=headers("bob")).status_code == 404


def test_metadata_only_test_store_fails_closed_without_outbox():
    app = create_hosted_app(verifier=TestVerifier(), store=TestPrivateStore())
    with TestClient(app) as client:
        sid = client.post("/api/v2/sessions", headers=headers("alice"), json={}).json()["id"]
        assert client.get(f"/api/v2/sessions/{sid}/events", headers=headers("alice")).status_code == 503
