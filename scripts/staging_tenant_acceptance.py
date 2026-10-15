"""Fail-closed black-box staging check for the *hosted metadata API*.

Requires HTTPS and two distinct pre-provisioned OIDC principals. It never
runs Agent tools or upgrades metadata acceptance into Agent approval.
Credentials are read only from environment variables; never printed.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable


@dataclass(frozen=True)
class Response:
    status: int
    payload: Any
    headers: dict[str, str]


class AcceptanceFailure(RuntimeError):
    pass


def _validate_base_url(url: str) -> str:
    if not isinstance(url, str) or not url:
        raise AcceptanceFailure("RIDGE_STAGING_BASE_URL is required")
    parts = urllib.parse.urlsplit(url)
    if (parts.scheme != "https" or not parts.hostname or parts.username
            or parts.password or parts.query or parts.fragment
            or parts.path not in {"", "/"}):
        raise AcceptanceFailure("Staging URL must be an HTTPS origin with no path, userinfo or query")
    return url.rstrip("/")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def http_client(origin: str) -> Callable:
    opener = urllib.request.build_opener(_NoRedirect)

    def call(method: str, path: str, *, token: str | None = None,
             body: dict | None = None, extra_headers: dict | None = None) -> Response:
        if not path.startswith("/api/") or "?" in path or "#" in path:
            raise AcceptanceFailure("Only explicit /api/ routes may be tested")
        headers = {"Accept": "application/json"}
        if token is not None:
            headers["Authorization"] = "Bearer " + token
        if extra_headers:
            headers.update(extra_headers)
        data = None
        if body is not None:
            data = json.dumps(body, separators=(",", ":")).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(origin + path, method=method,
                                         data=data, headers=headers)
        try:
            result = opener.open(request, timeout=10)
        except urllib.error.HTTPError as exc:
            result = exc
        except Exception as exc:
            raise AcceptanceFailure("Staging transport failed; inspect protected operator logs") from exc

        with result:
            raw = result.read(131073)
            if len(raw) > 131072:
                raise AcceptanceFailure("Staging API response exceeded 128 KiB")
            try:
                payload = json.loads(raw) if raw else None
            except (UnicodeError, ValueError) as exc:
                raise AcceptanceFailure("Staging API returned invalid JSON") from exc
            return Response(result.status, payload,
                            {k.lower(): v for k, v in result.headers.items()})
    return call


def _check(status: int, expected: int, description: str) -> None:
    if status != expected:
        raise AcceptanceFailure(f"{description}: expected HTTP {expected}, got HTTP {status}")


def run_acceptance(call: Callable, alice: str, bob: str) -> list[str]:
    """Destructive only to temporary sessions created in this run.

    The operator must supply distinct tenant/project/user principals; merely
    passing two arbitrary tokens is not proof of distinct real-world tenants.
    """
    if not alice or not bob or alice == bob:
        raise AcceptanceFailure("Two different OIDC bearer tokens are required")
    done = []
    created: list[tuple[str, str]] = []
    try:
        ready = call("GET", "/api/health/ready")
        _check(ready.status, 200, "database readiness")
        if not isinstance(ready.payload, dict) or ready.payload.get("status") != "ready":
            raise AcceptanceFailure("Readiness did not attest the private DB role")
        done.append("HOSTED-READY")

        _check(call("GET", "/api/v2/sessions").status, 401, "unauthenticated session listing")
        _check(call("GET", "/api/mcp/servers", token=alice).status, 404,
               "legacy MCP route must not exist")
        _check(call("GET", "/api/tools", token=alice).status, 404,
               "legacy host tools must not exist")
        done.append("HOSTED-ROUTES")

        first = call("POST", "/api/v2/sessions", token=alice,
                     body={"title": "staging-isolation-a"})
        _check(first.status, 201, "create A")
        if not isinstance(first.payload, dict) or not isinstance(first.payload.get("id"), str):
            raise AcceptanceFailure("Invalid session response for A")
        id_a = first.payload["id"]
        created.append((alice, id_a))
        second = call("POST", "/api/v2/sessions", token=bob,
                      body={"title": "staging-isolation-b"})
        _check(second.status, 201, "create B")
        if not isinstance(second.payload, dict) or not isinstance(second.payload.get("id"), str):
            raise AcceptanceFailure("Invalid session response for B")
        id_b = second.payload["id"]
        created.append((bob, id_b))

        _check(call("GET", f"/api/v2/sessions/{id_a}", token=bob).status,
               404, "B must not read A")
        _check(call("GET", f"/api/v2/sessions/{id_b}", token=alice).status,
               404, "A must not read B")
        _check(call("DELETE", f"/api/v2/sessions/{id_a}", token=bob).status,
               404, "B must not delete A")
        _check(call("PUT", f"/api/v2/sessions/{id_b}", token=alice,
                    body={"expected_version": 1, "snapshot": {"stolen": True}}).status,
               409, "A must not update B")
        spoofed = call("GET", f"/api/v2/sessions/{id_a}", token=bob,
                       extra_headers={"X-Tenant-ID": "trusted", "X-User-ID": "admin"})
        _check(spoofed.status, 404, "spoofed header must not elevate B")

        a_list = call("GET", "/api/v2/sessions", token=alice)
        b_list = call("GET", "/api/v2/sessions", token=bob)
        _check(a_list.status, 200, "A list")
        _check(b_list.status, 200, "B list")
        if not isinstance(a_list.payload, list) or not isinstance(b_list.payload, list):
            raise AcceptanceFailure("Invalid scoped session listing")
        a_ids = {x.get("id") for x in a_list.payload if isinstance(x, dict)}
        b_ids = {x.get("id") for x in b_list.payload if isinstance(x, dict)}
        if id_a not in a_ids or id_b in a_ids or id_b not in b_ids or id_a in b_ids:
            raise AcceptanceFailure("Cross-principal session list isolation failed")
        done.append("TENANT-ISOLATION")

        revised = call("PUT", f"/api/v2/sessions/{id_a}", token=alice,
                       body={"expected_version": 1, "snapshot": {"staging": True}})
        _check(revised.status, 200, "A CAS update")
        _check(call("PUT", f"/api/v2/sessions/{id_a}", token=alice,
                    body={"expected_version": 1, "snapshot": {}}).status,
               409, "stale CAS update")
        done.append("SESSION-CAS")
    finally:
        # Always attempt cleanup; failure is not proof of a valid release.
        for token, sid in created:
            try:
                response = call("DELETE", f"/api/v2/sessions/{sid}", token=token)
                if response.status != 204:
                    raise AcceptanceFailure("Temporary session cleanup failed")
            except Exception as exc:
                raise AcceptanceFailure("Staging cleanup incomplete; inspect staging sessions") from exc

    return done


def main() -> int:
    try:
        origin = _validate_base_url(os.environ.get("RIDGE_STAGING_BASE_URL", ""))
        alice = os.environ.get("RIDGE_STAGING_TOKEN_A", "")
        bob = os.environ.get("RIDGE_STAGING_TOKEN_B", "")
        checks = run_acceptance(http_client(origin), alice, bob)
    except AcceptanceFailure as exc:
        # Print only static/controlled acceptance messages; no secret, token
        # payload, session snapshot or HTTP response body enters stdout.
        print(json.dumps({"status": "NO-GO", "reason": str(exc)}, ensure_ascii=False))
        return 1
    print(json.dumps({"status": "PASS_METADATA_ONLY", "checks": checks}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
