"""Isolated hosted API / strict OIDC identity contract regression."""

from __future__ import annotations

import time
import uuid
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from core.memory import MemoryScope
from core.persistence import StoredPrivateSession, SessionVersionConflict
from desktop.service.hosted_app import create_hosted_app
from desktop.service.hosted_identity import (
    HostedIdentity, InvalidPrincipal, OIDCSettings, OIDCVerifier,
)


class TestVerifier:
    """Fake ONLY at the trusted server dependency boundary, not HTTP headers."""

    def verify(self, token):
        if token == "alice":
            return HostedIdentity(MemoryScope("t1", "u1", "p1"))
        if token == "bob":
            return HostedIdentity(MemoryScope("t2", "u1", "p1"))
        if token == "other-user":
            return HostedIdentity(MemoryScope("t1", "u2", "p1"))
        raise InvalidPrincipal("fake rejected token")


class TestPrivateStore:
    def __init__(self):
        self.records = {}

    @staticmethod
    def key(scope, sid):
        return scope.tenant_id, scope.user_id, scope.project_id, sid

    def create(self, scope, sid, *, title="", snapshot=None):
        key = self.key(scope, sid)
        if key in self.records:
            raise SessionVersionConflict("duplicate")
        r = StoredPrivateSession(sid, title, dict(snapshot or {}), 1)
        self.records[key] = r
        return r

    def get(self, scope, sid):
        return self.records.get(self.key(scope, sid))

    def list_private(self, scope):
        return [r for key, r in self.records.items() if key[:3] == (
            scope.tenant_id, scope.user_id, scope.project_id
        )]

    def save(self, scope, sid, snapshot, *, expected_version, title=None):
        k = self.key(scope, sid)
        old = self.records.get(k)
        if old is None or old.version != expected_version:
            raise SessionVersionConflict("stale")
        self.records[k] = StoredPrivateSession(
            sid, old.title if title is None else title,
            dict(snapshot), old.version + 1,
        )
        return old.version + 1

    def delete(self, scope, sid):
        return self.records.pop(self.key(scope, sid), None) is not None


def headers(token):
    return {"Authorization": "Bearer " + token}


def test_hosted_app_not_exposing_any_legacy_agent_or_plugin_routes():
    app = create_hosted_app(verifier=TestVerifier(), store=TestPrivateStore())
    routes = [getattr(r, "path", "") for r in app.routes]
    assert "/api/v2/sessions" in routes
    for path in ("/api/sessions", "/api/mcp/servers", "/api/plugins", "/api/tools"):
        assert path not in routes


def test_private_session_api_tenant_and_user_isolation():
    app = create_hosted_app(verifier=TestVerifier(), store=TestPrivateStore())
    with TestClient(app) as client:
        assert client.get("/api/health/live").status_code == 200
        assert client.post("/api/v2/sessions", json={}).status_code == 401
        assert client.get("/api/v2/sessions", headers=headers("invalid")).status_code == 401
        assert client.get("/api/v2/sessions", headers=headers("alice")).json() == []

        created = client.post("/api/v2/sessions", json={"title": "Private"},
                              headers=headers("alice"))
        assert created.status_code == 201
        sid = created.json()["id"]
        assert created.json()["version"] == 1
        assert len(sid) == 12
        assert client.get("/api/v2/sessions", headers=headers("alice")).json()[0]["id"] == sid

        for outsider in ("bob", "other-user"):
            assert client.get(f"/api/v2/sessions/{sid}", headers=headers(outsider)).status_code == 404
            assert client.get("/api/v2/sessions", headers=headers(outsider)).json() == []
            assert client.put(f"/api/v2/sessions/{sid}", headers=headers(outsider),
                              json={"expected_version": 1, "snapshot": {"stolen": True}}).status_code == 409
            assert client.delete(f"/api/v2/sessions/{sid}", headers=headers(outsider)).status_code == 404

        response = client.put(f"/api/v2/sessions/{sid}", headers=headers("alice"),
                              json={"expected_version": 1, "snapshot": {"answer": 42}})
        assert response.status_code == 200
        assert response.json()["version"] == 2
        assert client.put(f"/api/v2/sessions/{sid}", headers=headers("alice"),
                          json={"expected_version": 1, "snapshot": {}}).status_code == 409
        assert client.get(f"/api/v2/sessions/{sid}", headers=headers("alice")).json()["snapshot"] == {"answer": 42}
        assert client.delete(f"/api/v2/sessions/{sid}", headers=headers("alice")).status_code == 204
        assert client.get(f"/api/v2/sessions/{sid}", headers=headers("alice")).status_code == 404


def test_header_spoof_does_not_change_signed_scope():
    app = create_hosted_app(verifier=TestVerifier(), store=TestPrivateStore())
    with TestClient(app) as client:
        response = client.post("/api/v2/sessions", headers={
            **headers("alice"), "X-Tenant-ID": "t2", "X-User-ID": "u2",
        }, json={"title": "not shared"})
        sid = response.json()["id"]
        assert client.get(f"/api/v2/sessions/{sid}", headers=headers("alice")).status_code == 200
        assert client.get(f"/api/v2/sessions/{sid}", headers=headers("bob")).status_code == 404


def test_hosted_oidc_config_rejects_insecure_jwks():
    with pytest.raises(ValueError):
        OIDCSettings("https://issuer.example", "ridge", "http://issuer.example/keys")
    with pytest.raises(ValueError):
        OIDCSettings("http://issuer.example", "ridge", "https://issuer.example/keys")
    with pytest.raises(ValueError):
        OIDCSettings("https://issuer.example", "ridge", "https://evil:pw@issuer.example/keys")
    with pytest.raises(ValueError):
        OIDCSettings("https://issuer.example", "ridge", "https://issuer.example/keys", max_token_age_seconds=0)


def test_real_jwt_signature_issuer_audience_scope_and_expiry():
    jwt = pytest.importorskip("jwt")
    cryptography = pytest.importorskip("cryptography")
    from cryptography.hazmat.primitives.asymmetric import rsa

    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public = private.public_key()
    settings = OIDCSettings("https://issuer.example", "ridge-api", "https://issuer.example/keys")
    verifier = OIDCVerifier(settings)
    verifier._jwks = SimpleNamespace(
        get_signing_key_from_jwt=lambda _: SimpleNamespace(key=public),
    )
    now = int(time.time())
    valid = {
        "iss": settings.issuer, "aud": settings.audience, "sub": "subject-1",
        "iat": now, "exp": now + 300, "ridge_tenant": "t1", "ridge_project": "p1",
    }

    def encoded(claims, key=private, algorithm="RS256"):
        return jwt.encode(claims, key, algorithm=algorithm)

    result = verifier.verify(encoded(valid))
    assert result.scope == MemoryScope("t1", "subject-1", "p1")
    for changes in (
        {"iss": "https://evil.example"},
        {"aud": "other"},
        {"exp": now - 600},
        {"iat": now - 6000},
        {"ridge_tenant": None},
        {"ridge_tenant": "../escape"},
        {"ridge_project": None},
        {"sub": None},
    ):
        with pytest.raises(InvalidPrincipal):
            verifier.verify(encoded({**valid, **changes}))
    with pytest.raises(InvalidPrincipal):
        verifier.verify(encoded(valid, "untrusted-secret", "HS256"))
    with pytest.raises(InvalidPrincipal):
        verifier.verify("bad.jwt.token")


def test_hosted_liveness_is_not_a_false_readiness_claim():
    app = create_hosted_app(verifier=TestVerifier(), store=TestPrivateStore())
    with TestClient(app) as client:
        assert client.get("/api/health/live").status_code == 200
        # A storage adapter without security probe cannot declare readiness.
        assert client.get("/api/health/ready").status_code == 503


def test_local_web_refuses_hosted_mode(tmp_path, monkeypatch):
    from desktop.service.app import create_app
    monkeypatch.setenv("RIDGE_DEPLOYMENT_MODE", "hosted")
    with pytest.raises(RuntimeError, match="Legacy local Agent API"):
        create_app(project_root=str(tmp_path), agent_factory=lambda: None)


def test_hosted_rejects_oversized_request_before_json_parsing():
    app = create_hosted_app(verifier=TestVerifier(), store=TestPrivateStore())
    with TestClient(app) as client:
        response = client.post(
            "/api/v2/sessions",
            content=b"x" * 4_300_001,
            headers={**headers("alice"), "Content-Type": "application/json"},
        )
        assert response.status_code == 413
