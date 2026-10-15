"""Live PostgreSQL RLS / optimistic-concurrency tests (optional local DSN).

The CI PostgreSQL job supplies a NON-SUPERUSER test role. Unit runs without
RIDGE_TEST_POSTGRES_DSN skip these tests; do not substitute sqlite for RLS.
"""

from __future__ import annotations

import os
import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest

from core.memory import MemoryScope
from core.persistence import PostgresPrivateSessionStore, SessionVersionConflict


@pytest.fixture
def backend():
    psycopg = pytest.importorskip("psycopg")
    dsn = os.environ.get("RIDGE_TEST_POSTGRES_DSN")
    if not dsn:
        pytest.skip("Explicit RIDGE_TEST_POSTGRES_DSN required")
    with psycopg.connect(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT rolsuper, rolbypassrls FROM pg_roles
                   WHERE rolname = current_user"""
            )
            assert cur.fetchone() == (False, False), "Never verify RLS as superuser"
            cur.execute(
                """SELECT count(*) FROM pg_policies
                   WHERE schemaname='ridge'
                   AND tablename='private_sessions'"""
            )
            assert cur.fetchone()[0] > 0, "Run schema migration first"
    return PostgresPrivateSessionStore(dsn)


def scope(tenant="t1", user="u1", project="p1"):
    return MemoryScope(tenant_id=tenant, user_id=user, project_id=project)


def test_scope_isolation_and_optimistic_versioning(backend):
    sid = uuid.uuid4().hex[:12]
    owner = scope()
    other_tenant = scope(tenant="t2")
    other_user = scope(user="u2")
    other_project = scope(project="p2")
    try:
        first = backend.create(owner, sid, title="private", snapshot={"messages": ["ok"]})
        assert first.version == 1
        assert first.title == "private"
        assert backend.get(owner, sid).snapshot == {"messages": ["ok"]}

        for outsider in (other_tenant, other_user, other_project):
            assert backend.get(outsider, sid) is None
            with pytest.raises(SessionVersionConflict):
                backend.save(outsider, sid, {"leak": "try"}, expected_version=1)
            assert not backend.delete(outsider, sid)

        assert backend.save(owner, sid, {"messages": ["updated"]}, expected_version=1) == 2
        assert backend.get(owner, sid).snapshot == {"messages": ["updated"]}
        with pytest.raises(SessionVersionConflict):
            backend.save(owner, sid, {"stale": True}, expected_version=1)
    finally:
        backend.delete(owner, sid)


def test_same_session_id_is_safe_between_tenants(backend):
    sid = uuid.uuid4().hex[:12]
    alice = scope(tenant="one", user="alice")
    bob = scope(tenant="two", user="alice")
    try:
        backend.create(alice, sid, snapshot={"marker": "one"})
        backend.create(bob, sid, snapshot={"marker": "two"})
        assert backend.get(alice, sid).snapshot == {"marker": "one"}
        assert backend.get(bob, sid).snapshot == {"marker": "two"}
        assert backend.delete(alice, sid)
        assert backend.get(bob, sid).snapshot == {"marker": "two"}
    finally:
        backend.delete(alice, sid)
        backend.delete(bob, sid)


def test_concurrent_compare_and_swap_has_one_winner(backend):
    sid = uuid.uuid4().hex[:12]
    owner = scope(tenant="cas", user="user", project="project")
    try:
        backend.create(owner, sid, snapshot={"version": 1})

        def save(i):
            try:
                return backend.save(owner, sid, {"winner": i}, expected_version=1)
            except SessionVersionConflict:
                return None

        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(save, range(8)))
        assert results.count(2) == 1
        assert results.count(None) == 7
        assert backend.get(owner, sid).version == 2
    finally:
        backend.delete(owner, sid)


def test_direct_unscoped_database_read_is_denied(backend):
    psycopg = pytest.importorskip("psycopg")
    sid = uuid.uuid4().hex[:12]
    owner = scope(tenant="secret", user="private", project="work")
    try:
        backend.create(owner, sid, snapshot={"private": True})
        with psycopg.connect(os.environ["RIDGE_TEST_POSTGRES_DSN"]) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT COUNT(*) FROM ridge.private_sessions WHERE session_id=%s",
                    (sid,),
                )
                assert cur.fetchone() == (0,)
                cur.execute(
                    """UPDATE ridge.private_sessions SET title='hacked'
                       WHERE session_id=%s""",
                    (sid,),
                )
                assert cur.rowcount == 0
        assert backend.get(owner, sid).title == ""
    finally:
        backend.delete(owner, sid)


def test_invalid_scope_and_id_fail_before_database(backend):
    with pytest.raises(TypeError):
        backend.get(None, "123456789abc")
    for bad in ("../bad", "x" * 13, "uppercaseAB12"):
        with pytest.raises(ValueError):
            backend.get(scope(), bad)


def test_real_http_routing_uses_signed_principal_scope(backend):
    """Live PostgreSQL permissions + isolated HTTP endpoints together."""
    from fastapi.testclient import TestClient
    from desktop.service.hosted_app import create_hosted_app
    from tests.test_hosted_identity_api import TestVerifier, headers

    psycopg = pytest.importorskip("psycopg")
    admin_dsn = os.environ.get("RIDGE_TEST_POSTGRES_ADMIN_DSN")
    if not admin_dsn:
        pytest.skip("Trusted isolated CI provisioning DSN required")
    # Only this CI admin role can provision signed principals. The runtime
    # identity is read-only and can never self-enroll.
    members = [
        ("t1", "p1", "u1"), ("t2", "p1", "u1"), ("t1", "p1", "u2"),
    ]
    with psycopg.connect(admin_dsn) as conn, conn.cursor() as cur:
        for member in members:
            cur.execute(
                """INSERT INTO ridge.private_project_memberships
                   (tenant_id,project_id,user_id,enabled)
                   VALUES (%s,%s,%s,TRUE)
                   ON CONFLICT (tenant_id,project_id,user_id)
                   DO UPDATE SET enabled=TRUE, updated_at=now()""",
                member,
            )

    try:
        app = create_hosted_app(verifier=TestVerifier(), store=backend)
        with TestClient(app) as client:
            response = client.post("/api/v2/sessions", json={"title": "live-private"},
                                   headers=headers("alice"))
            assert response.status_code == 201
            sid = response.json()["id"]
            try:
                assert client.get("/api/v2/sessions", headers=headers("alice")).status_code == 200
                assert client.get(f"/api/v2/sessions/{sid}", headers=headers("bob")).status_code == 404
                assert client.get(f"/api/v2/sessions/{sid}", headers=headers("other-user")).status_code == 404
                response = client.put(f"/api/v2/sessions/{sid}",
                                      headers=headers("alice"),
                                      json={"expected_version": 1, "snapshot": {"phase": "tested"}})
                assert response.status_code == 200
                assert response.json()["version"] == 2
                assert client.put(f"/api/v2/sessions/{sid}", headers=headers("bob"),
                                  json={"expected_version": 2, "snapshot": {"bad": 1}}).status_code == 409
                assert backend.get(scope("t1", "u1", "p1"), sid).snapshot == {"phase": "tested"}
            finally:
                assert client.delete(f"/api/v2/sessions/{sid}",
                                     headers=headers("alice")).status_code == 204
    finally:
        with psycopg.connect(admin_dsn) as conn, conn.cursor() as cur:
            for member in members:
                cur.execute(
                    """DELETE FROM ridge.private_project_memberships
                       WHERE tenant_id=%s AND project_id=%s AND user_id=%s""",
                    member,
                )


def test_production_probe_requires_unprivileged_rls_runtime_role(backend):
    assert backend.production_probe() is True


def test_runtime_membership_is_authoritative_and_revocable(backend):
    psycopg = pytest.importorskip("psycopg")
    admin_dsn = os.environ.get("RIDGE_TEST_POSTGRES_ADMIN_DSN")
    if not admin_dsn:
        pytest.skip("Admin DSN for isolated CI membership provisioning required")
    tag = uuid.uuid4().hex[:12]
    scoped = scope(tenant="membership-" + tag, user="user-" + tag,
                   project="project-" + tag)
    args = (scoped.tenant_id, scoped.project_id, scoped.user_id)
    try:
        with psycopg.connect(admin_dsn) as conn, conn.cursor() as cur:
            cur.execute(
                """INSERT INTO ridge.private_project_memberships
                   (tenant_id,project_id,user_id,enabled)
                   VALUES (%s,%s,%s,TRUE)""",
                args,
            )
        assert backend.authorize_membership(scoped) is True
        assert backend.authorize_membership(
            scope(tenant=scoped.tenant_id, user="other", project=scoped.project_id)
        ) is False
        # Runtime has *read-only* membership privilege; disabling access
        # requires a privileged provisioning process.
        with psycopg.connect(os.environ["RIDGE_TEST_POSTGRES_DSN"]) as conn:
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                with conn.cursor() as cur:
                    backend._scope(cur, scoped)
                    cur.execute(
                        """UPDATE ridge.private_project_memberships
                              SET enabled=FALSE
                            WHERE tenant_id=%s AND project_id=%s AND user_id=%s""",
                        args,
                    )

        with psycopg.connect(admin_dsn) as conn, conn.cursor() as cur:
            cur.execute(
                """UPDATE ridge.private_project_memberships SET enabled=FALSE
                   WHERE tenant_id=%s AND project_id=%s AND user_id=%s""",
                args,
            )
        assert backend.authorize_membership(scoped) is False
    finally:
        with psycopg.connect(admin_dsn) as conn, conn.cursor() as cur:
            cur.execute(
                """DELETE FROM ridge.private_project_memberships
                   WHERE tenant_id=%s AND project_id=%s AND user_id=%s""",
                args,
            )


def test_hosted_readiness_rejects_missing_membership_guards(backend):
    # The real runtime role must not own or mutate the membership table.
    assert backend.production_probe() is True
