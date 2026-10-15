"""Deterministic Agent evaluator never calls a real model or leaks fixture text."""
from __future__ import annotations

import json

import pytest

from scripts.agent_eval import CASES, Case, _run_case, main, run_eval


def test_offline_evaluation_scores_real_side_effects():
    report = run_eval()
    assert report["result"] == "pass", report
    assert report["mode"] == "offline_scripted_no_provider"
    assert report["passed"] == report["case_count"] == 5
    assert {c["id"] for c in report["cases"]} == {
        "cross_file_refactor", "tool_error_recovery",
        "multiturn_continuation", "outside_workspace_denial",
        "context_compaction_continuity",
    }
    assert report["model_calls"] == 3 + 3 + 4 + 2


def test_evaluation_artifact_mismatch_is_not_success():
    original = CASES[1]
    failed = Case(
        id=original.id, initial=original.initial,
        expected={"report.txt": "incorrect expected artifact"},
        preserved=original.preserved, prompts=original.prompts,
        script=original.script,
    )
    report = _run_case(failed)
    assert not report["pass"]
    assert "expected_artifact_mismatch" in report["errors"]


def test_json_is_machine_readable_redacted_and_case_selectable(capsys):
    assert main(["--case", "outside_workspace_denial", "--json"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["case_count"] == 1
    assert report["cases"][0]["id"] == "outside_workspace_denial"
    serialized = json.dumps(report)
    for forbidden in ("must-not-write", "protected", "Goal: keep", "missing.txt"):
        assert forbidden not in serialized


def test_unknown_case_fails_before_any_execution():
    with pytest.raises(ValueError, match="Unknown evaluation case"):
        run_eval(("not-real",))
    with pytest.raises(ValueError):
        run_eval(tuple())


def test_each_fixture_has_unique_case_id():
    assert len({c.id for c in CASES}) == len(CASES)
    assert all(c.script and c.prompts for c in CASES)
