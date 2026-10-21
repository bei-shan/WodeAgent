"""Safe offline black-box staging acceptance tests against hosted ASGI app."""

from __future__ import annotations

import pytest
from types import SimpleNamespace
from fastapi.testclient import TestClient

from desktop.service.hosted_app import create_hosted_app
from scripts.staging_tenant_acceptance import (
    AcceptanceFailure,
    Response,
    _validate_base_url,
    run_acceptance,
)
from tests.test_hosted_identity_api import TestVerifier, TestPrivateStore


def _as_client(http):
    def request(method, path, *, token=None, body=None, extra_headers=None):
        headers = dict(extra_headers or {})
        if token is not None:
            headers["Authorization"] = "Bearer " + token
        result = http.request(method, path, headers=headers, json=body)
        return Response(result.status_code, result.json() if result.content else None,
                        dict(result.headers))
    return request


def test_staging_acceptance_contract_with_real_hosted_routes():
    store = TestPrivateStore()
    store.production_probe = lambda: True
    # This is an OFFLINE transport/identity contract test; the real
    # PostgreSQL workflow separately asserts event_security_probe() against
    # forced RLS and the least-privileged runtime role.
    fake_outbox = SimpleNamespace(event_security_probe=lambda: True)
    app = create_hosted_app(verifier=TestVerifier(), store=store, turn_events=fake_outbox)
    with TestClient(app) as client:
        result = run_acceptance(_as_client(client), "alice", "bob")
    assert result == [
        "HOSTED-READY", "HOSTED-ROUTES", "TENANT-ISOLATION", "SESSION-CAS",
    ]
    assert store.records == {}


def test_duplicate_principal_or_no_ready_is_no_go():
    store = TestPrivateStore()
    app = create_hosted_app(verifier=TestVerifier(), store=store)
    with TestClient(app) as client:
        with pytest.raises(AcceptanceFailure, match="Two different"):
            run_acceptance(_as_client(client), "alice", "alice")
        with pytest.raises(AcceptanceFailure, match="database readiness"):
            run_acceptance(_as_client(client), "alice", "bob")


@pytest.mark.parametrize("url", [
    "", "http://staging.example", "https://u:pass@staging.example",
    "https://staging.example/api", "https://staging.example/?token=secret",
    "https://staging.example/#frag",
])
def test_staging_origin_rejects_insecure_or_credentialed_urls(url):
    with pytest.raises(AcceptanceFailure):
        _validate_base_url(url)


def test_valid_staging_origin_is_https():
    assert _validate_base_url("https://staging.example/") == "https://staging.example"
