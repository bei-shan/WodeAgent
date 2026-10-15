"""Release candidate acceptance runner: bounded, secret-redacted evidence.

No live credential or cloud billing is needed for the component suite.
External scopes are explicit; skipped tests are NEVER recorded as passed.
Evidence is bound to the checked-out commit and GitHub Actions run id.
This runner CANNOT mark hosted multi-tenant agent execution production-ready.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from datetime import datetime, timezone
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
SCOPES = ("component", "docker", "redis", "e2b", "boxlite", "staging", "soak")
REQUIRED_FOR_PRODUCTION = ("component", "docker", "redis", "e2b", "boxlite", "staging", "soak")
STATUS = {"PASS", "PASS_METADATA_ONLY", "BLOCKED", "FAIL"}
SCHEMA_VERSION = 1
# Test names are literals; never interpolate user/LLM input into shell commands.
COMPONENT_TESTS = (
    "tests/test_sandbox_e2b.py",
    "tests/test_sandbox_e2b_redis.py",
    "tests/test_sandbox_e2b_guest_files.py",
    "tests/test_sandbox_e2b_reconciliation.py",
    "tests/test_sandbox_e2b_artifacts.py",
    "tests/test_hosted_identity_api.py",
    "tests/test_staging_tenant_acceptance.py",
)


def commit_sha() -> str:
    """Never infer the release candidate SHA from a PR title or artifact name."""
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=ROOT,
        text=True, capture_output=True, timeout=8, check=True,
    )
    value = result.stdout.strip().lower()
    if len(value) != 40 or any(ch not in "0123456789abcdef" for ch in value):
        raise RuntimeError("Cannot verify a 40-character Git commit")
    return value


def junit_counts(path: Path) -> dict[str, int]:
    """Strictly reject empty, malformed, missing or skipped integration suites."""
    root = ET.parse(path).getroot()
    suites = [root] if root.tag == "testsuite" else list(root.iter("testsuite"))
    if not suites:
        raise ValueError("Missing JUnit testsuite")
    # Each test suite can contain nested suites; count leaf test cases once.
    cases = list(root.iter("testcase"))
    if not cases:
        raise ValueError("No tests actually executed")
    return {
        "tests": len(cases),
        "skipped": sum(bool(list(c.iter("skipped"))) for c in cases),
        "failures": sum(bool(list(c.iter("failure"))) for c in cases),
        "errors": sum(bool(list(c.iter("error"))) for c in cases),
    }


def _result(scope: str, sha: str, status: str, reason: str,
            duration: float, counts: dict | None = None) -> dict:
    return {
        "schema_version": SCHEMA_VERSION,
        "scope": scope,
        "source_sha": sha,
        "github_run_id": os.environ.get("GITHUB_RUN_ID", "")[:32],
        "recorded_utc": datetime.now(timezone.utc).isoformat(),
        "duration_seconds": round(duration, 2),
        "status": status,
        "reason_code": reason,
        "counts": counts or {},
    }


def _pytest(scope: str, xml: Path, timeout: int) -> tuple[list[str], dict[str, str]]:
    env = {}
    if scope == "component":
        return [
            sys.executable, "-m", "pytest", *COMPONENT_TESTS,
            "-k", "not test_real_redis_atomic_capacity_and_fenced_release",
            "-q", "--disable-warnings", "--junitxml=" + str(xml),
        ], env
    if scope == "docker":
        env["AGENT_DOCKER_INTEGRATION"] = "1"
        return [sys.executable, "-m", "pytest",
                "tests/test_sandbox_docker_integration.py", "-q",
                "--junitxml=" + str(xml)], env
    if scope == "redis":
        env["AGENT_E2B_REDIS_INTEGRATION"] = "1"
        return [sys.executable, "-m", "pytest",
                "tests/test_sandbox_e2b_redis.py",
                "-k", "test_real_redis_atomic_capacity_and_fenced_release",
                "-q", "--junitxml=" + str(xml)], env
    if scope == "e2b":
        env["AGENT_E2B_INTEGRATION"] = "1"
        env["AGENT_E2B_ALLOW_BILLING"] = "1"
        return [sys.executable, "-m", "pytest",
                "tests/test_sandbox_e2b_integration.py", "-q",
                "--junitxml=" + str(xml)], env
    if scope == "boxlite":
        env["AGENT_BOXLITE_INTEGRATION"] = "1"
        return [sys.executable, "-m", "pytest",
                "tests/test_sandbox_boxlite_integration.py", "-q",
                "--junitxml=" + str(xml)], env
    raise ValueError("Invalid pytest scope")


def _precondition(scope: str, approved_billing: bool) -> str | None:
    if scope == "redis" and not os.environ.get("E2B_REDIS_URL"):
        return "MISSING_REDIS_TEST_CREDENTIAL"
    if scope == "e2b":
        if not approved_billing:
            return "E2B_BILLING_NOT_APPROVED"
        if not os.environ.get("E2B_API_KEY"):
            return "MISSING_E2B_TEST_CREDENTIAL"
    if scope == "staging" and any(
        not os.environ.get(k) for k in (
            "RIDGE_STAGING_BASE_URL", "RIDGE_STAGING_TOKEN_A", "RIDGE_STAGING_TOKEN_B",
        )
    ):
        return "MISSING_STAGING_IDENTITY_OR_ORIGIN"
    if scope == "boxlite" and not os.environ.get("RIDGE_BOXLITE_HARDWARE_APPROVED"):
        return "BOXLITE_HARDWARE_NOT_APPROVED"
    return None


def run_scope(scope: str, sha: str, workdir: Path,
              *, approved_billing: bool = False) -> dict:
    start = time.monotonic()
    blocker = _precondition(scope, approved_billing)
    if blocker:
        return _result(scope, sha, "BLOCKED", blocker, time.monotonic() - start)
    env = dict(os.environ)
    xml = workdir / (scope + ".xml")
    try:
        if scope in {"component", "docker", "redis", "e2b", "boxlite"}:
            command, additions = _pytest(scope, xml, 420)
            env.update(additions)
            timeout = 420 if scope != "e2b" else 600
        elif scope == "staging":
            command = [sys.executable, "scripts/staging_tenant_acceptance.py"]
            timeout = 120
        elif scope == "soak":
            command = [sys.executable, "scripts/agent_soak.py",
                       "--duration-seconds", "1800", "--json"]
            timeout = 2000
        else:
            raise ValueError("Unsupported acceptance scope")

        # Never stream subprocess output into public CI logs or report files:
        # tool output, request diagnostics and environment details may be secret.
        finished = subprocess.run(
            command, cwd=ROOT, env=env, capture_output=True, text=True,
            timeout=timeout, check=False,
        )
        counts = {}
        if scope in {"component", "docker", "redis", "e2b", "boxlite"}:
            if not xml.is_file():
                return _result(scope, sha, "FAIL", "MISSING_JUNIT_EVIDENCE",
                               time.monotonic() - start)
            counts = junit_counts(xml)
            if finished.returncode != 0 or counts["failures"] or counts["errors"]:
                status, reason = "FAIL", "PYTEST_FAILED"
            elif counts["skipped"]:
                status, reason = "BLOCKED", "REAL_TEST_WAS_SKIPPED"
            else:
                status, reason = "PASS", "REAL_TESTS_EXECUTED" if scope != "component" else "OFFLINE_TESTS_PASSED"
        elif scope == "staging":
            try:
                payload = json.loads(finished.stdout.strip())
            except (ValueError, TypeError):
                payload = None
            if (finished.returncode == 0 and isinstance(payload, dict)
                    and payload.get("status") == "PASS_METADATA_ONLY"
                    and set(payload.get("checks", [])) >= {
                        "HOSTED-READY", "HOSTED-ROUTES",
                        "TENANT-ISOLATION", "SESSION-CAS",
                    }):
                status, reason = "PASS_METADATA_ONLY", "METADATA_CONTRACT_ONLY"
            else:
                status, reason = "FAIL", "STAGING_BLACKBOX_FAILED"
        else:
            try:
                payload = json.loads(finished.stdout)
            except (ValueError, TypeError):
                payload = None
            # Offline soak is an extra evidence slice, not a 24-hour hosted run.
            if finished.returncode == 0 and isinstance(payload, dict):
                status, reason = "PASS", "OFFLINE_30M_SOAK_ONLY"
            else:
                status, reason = "FAIL", "OFFLINE_SOAK_FAILED"
        return _result(scope, sha, status, reason, time.monotonic() - start, counts)
    except subprocess.TimeoutExpired:
        return _result(scope, sha, "FAIL", "TEST_TIMED_OUT", time.monotonic() - start)
    except (OSError, ValueError, ET.ParseError):
        return _result(scope, sha, "FAIL", "INVALID_OR_MISSING_TEST_EVIDENCE",
                       time.monotonic() - start)


def summary(sha: str, directory: Path) -> dict:
    gates = {}
    for scope in REQUIRED_FOR_PRODUCTION:
        path = directory / (scope + ".json")
        if not path.is_file():
            gates[scope] = {"status": "BLOCKED", "reason_code": "NO_EVIDENCE"}
            continue
        try:
            item = json.loads(path.read_text(encoding="utf-8"))
            if (not isinstance(item, dict) or item.get("scope") != scope
                    or item.get("schema_version") != SCHEMA_VERSION
                    or item.get("source_sha") != sha
                    or item.get("status") not in STATUS):
                raise ValueError("Untrusted or mismatched evidence")
            gates[scope] = {
                "status": item["status"],
                "reason_code": item.get("reason_code", "UNSPECIFIED"),
                "source_sha": sha,
                "counts": item.get("counts", {}),
            }
        except (OSError, ValueError, TypeError):
            gates[scope] = {"status": "BLOCKED", "reason_code": "INVALID_EVIDENCE"}
    # Never silently promote test infrastructure into approval of unavailable
    # hosted Agent execution, multi-tenant auth or manual human security signoff.
    return {
        "schema_version": SCHEMA_VERSION,
        "source_sha": sha,
        "gate_evidence": gates,
        "production_multi_tenant": "NO-GO",
        "production_blockers": [
            "HOSTED_AGENT_IDENTITY_NOT_END_TO_END",
            "MULTI_GATEWAY_FAILOVER_NOT_ACCEPTED",
            "LIVE_24H_MIXED_TENANT_SOAK_MISSING",
            "SECURITY_OPERATIONS_SIGNOFF_MISSING",
        ],
    }


def markdown_report(document: dict) -> str:
    sha = document["source_sha"]
    lines = [
        "# Ridge Code acceptance evidence",
        "",
        f"Commit: {sha}",
        "",
        "| Gate | Evidence | Reason |",
        "|---|---|---|",
    ]
    for scope, item in document["gate_evidence"].items():
        lines.append(f"| {scope} | {item['status']} | {item['reason_code']} |")
    lines.extend([
        "",
        "**Public multi-tenant Agent production decision: NO-GO.**",
        "",
        "Passing component/metadata tests is not hosted Agent authorization.",
        "Real external tests must run on approved isolated infrastructure;",
        "missing/skipped/mismatched evidence is never counted as PASS.",
        "",
    ])
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Strict release acceptance evidence")
    parser.add_argument("--scope", required=True, choices=(*SCOPES, "summary"))
    parser.add_argument("--output-dir", default="acceptance-evidence")
    parser.add_argument("--approve-e2b-billing", action="store_true")
    args = parser.parse_args(argv)
    sha = commit_sha()
    directory = Path(args.output_dir)
    directory.mkdir(parents=True, exist_ok=True)
    if args.scope == "summary":
        result = summary(sha, directory)
        (directory / "summary.json").write_text(
            json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8",
        )
        (directory / "summary.md").write_text(
            markdown_report(result), encoding="utf-8",
        )
        print(markdown_report(result))
        return 0  # Summary generation is not production authorization.
    outcome = run_scope(
        args.scope, sha, directory, approved_billing=args.approve_e2b_billing,
    )
    (directory / (args.scope + ".json")).write_text(
        json.dumps(outcome, indent=2, sort_keys=True) + "\n", encoding="utf-8",
    )
    # Redacted static fields only. Never print subprocess stdout/stderr.
    print(json.dumps({
        "scope": outcome["scope"], "source_sha": outcome["source_sha"],
        "status": outcome["status"], "reason_code": outcome["reason_code"],
        "counts": outcome["counts"],
    }, sort_keys=True))
    return 0 if outcome["status"] in ("PASS", "PASS_METADATA_ONLY") else 2


if __name__ == "__main__":
    sys.exit(main())
