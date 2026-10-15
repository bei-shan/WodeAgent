"""Fail-closed release acceptance: test skips, SHA provenance and approval."""
from __future__ import annotations

import json
import subprocess
import xml.etree.ElementTree as ET

import pytest

from scripts import release_acceptance as gate


SHA = "f" * 40


def test_junit_counts_refuses_zero_tests_and_skips(tmp_path):
    xml = tmp_path / "cases.xml"
    xml.write_text(
        '<testsuite tests="3"><testcase name="a"/>'
        '<testcase name="b"><skipped/></testcase>'
        '<testcase name="c"><failure/></testcase></testsuite>',
        encoding="utf-8",
    )
    assert gate.junit_counts(xml) == {
        "tests": 3, "skipped": 1, "failures": 1, "errors": 0,
    }
    xml.write_text("<testsuite tests='0'/>")
    with pytest.raises(ValueError, match="No tests"):
        gate.junit_counts(xml)


def test_no_credentials_or_billing_is_blocked_without_spawning(tmp_path, monkeypatch):
    for name in ("E2B_API_KEY", "E2B_REDIS_URL", "RIDGE_STAGING_BASE_URL",
                 "RIDGE_STAGING_TOKEN_A", "RIDGE_STAGING_TOKEN_B"):
        monkeypatch.delenv(name, raising=False)
    def never_spawn(*a, **k):
        raise AssertionError("must not execute a cloud or staging test")
    monkeypatch.setattr(gate.subprocess, "run", never_spawn)
    assert gate.run_scope("e2b", SHA, tmp_path)["reason_code"] == "E2B_BILLING_NOT_APPROVED"
    assert gate.run_scope(
        "e2b", SHA, tmp_path, approved_billing=True,
    )["reason_code"] == "MISSING_E2B_TEST_CREDENTIAL"
    assert gate.run_scope("redis", SHA, tmp_path)["status"] == "BLOCKED"
    assert gate.run_scope("staging", SHA, tmp_path)["status"] == "BLOCKED"


def test_explicit_e2b_approval_still_requires_nonempty_executed_tests(
        tmp_path, monkeypatch):
    monkeypatch.setenv("E2B_API_KEY", "fake-do-not-call-cloud")
    def skipped(command, **kwargs):
        xml_arg = next(x for x in command if x.startswith("--junitxml="))
        xml = xml_arg.partition("=")[-1]
        with open(xml, "w", encoding="utf-8") as stream:
            stream.write('<testsuite><testcase name="live"><skipped/></testcase></testsuite>')
        return subprocess.CompletedProcess(command, 0, "", "")
    monkeypatch.setattr(gate.subprocess, "run", skipped)
    record = gate.run_scope("e2b", SHA, tmp_path, approved_billing=True)
    assert record["status"] == "BLOCKED"
    assert record["reason_code"] == "REAL_TEST_WAS_SKIPPED"
    assert record["counts"]["tests"] == 1


def test_failing_real_integration_cannot_be_counted_pass(tmp_path, monkeypatch):
    monkeypatch.setenv("E2B_REDIS_URL", "redis://secret.example:6379/0")
    def failed(command, **kwargs):
        xml_arg = next(x for x in command if x.startswith("--junitxml="))
        with open(xml_arg.partition("=")[-1], "w", encoding="utf-8") as stream:
            stream.write('<testsuite><testcase name="live"><failure/></testcase></testsuite>')
        return subprocess.CompletedProcess(command, 1, "a secret", "secret exception")
    monkeypatch.setattr(gate.subprocess, "run", failed)
    record = gate.run_scope("redis", SHA, tmp_path)
    assert record["status"] == "FAIL"
    assert "secret" not in json.dumps(record)
    assert "redis://" not in json.dumps(record)


def test_component_tests_never_select_live_redis_test(tmp_path):
    argv, env = gate._pytest("component", tmp_path / "x.xml", 420)
    assert "-k" in argv
    assert "not test_real_redis_atomic_capacity_and_fenced_release" in argv
    assert not env


def test_staging_metadata_pass_does_not_upgrade_agent_release(tmp_path, monkeypatch):
    monkeypatch.setenv("RIDGE_STAGING_BASE_URL", "https://staging.example")
    monkeypatch.setenv("RIDGE_STAGING_TOKEN_A", "test-A")
    monkeypatch.setenv("RIDGE_STAGING_TOKEN_B", "test-B")
    def fake(command, **kwargs):
        data = {"status": "PASS_METADATA_ONLY",
                "checks": ["HOSTED-READY", "HOSTED-ROUTES",
                           "TENANT-ISOLATION", "SESSION-CAS"]}
        return subprocess.CompletedProcess(command, 0, json.dumps(data), "")
    monkeypatch.setattr(gate.subprocess, "run", fake)
    outcome = gate.run_scope("staging", SHA, tmp_path)
    assert outcome["status"] == "PASS_METADATA_ONLY"
    (tmp_path / "staging.json").write_text(json.dumps(outcome))
    summary = gate.summary(SHA, tmp_path)
    assert summary["production_multi_tenant"] == "NO-GO"
    assert summary["gate_evidence"]["staging"]["status"] == "PASS_METADATA_ONLY"


def test_mismatched_sha_invalid_data_and_missing_gates_are_blocked(tmp_path):
    record = gate._result("redis", "0" * 40, "PASS", "REAL_TESTS_EXECUTED", 0.1)
    (tmp_path / "redis.json").write_text(json.dumps(record))
    (tmp_path / "e2b.json").write_text("{not JSON")
    outcomes = gate.summary(SHA, tmp_path)
    assert outcomes["gate_evidence"]["redis"]["reason_code"] == "INVALID_EVIDENCE"
    assert outcomes["gate_evidence"]["e2b"]["reason_code"] == "INVALID_EVIDENCE"
    assert outcomes["gate_evidence"]["docker"]["reason_code"] == "NO_EVIDENCE"
    assert outcomes["production_multi_tenant"] == "NO-GO"


def test_component_gate_summary_cli_has_no_production_go(monkeypatch, tmp_path):
    monkeypatch.setattr(gate, "commit_sha", lambda: SHA)
    outcome = gate._result("component", SHA, "PASS", "OFFLINE_TESTS_PASSED", 0.1)
    (tmp_path / "component.json").write_text(json.dumps(outcome))
    result = gate.main(["--scope", "summary", "--output-dir", str(tmp_path)])
    assert result == 0
    emitted = json.loads((tmp_path / "summary.json").read_text())
    assert emitted["production_multi_tenant"] == "NO-GO"
    assert (tmp_path / "summary.md").is_file()


def test_explicit_approval_must_not_be_satisfied_by_environment_only(tmp_path, monkeypatch):
    monkeypatch.setenv("E2B_API_KEY", "fake")
    monkeypatch.setenv("AGENT_E2B_ALLOW_BILLING", "1")
    assert gate.run_scope(
        "e2b", SHA, tmp_path, approved_billing=False,
    )["status"] == "BLOCKED"
