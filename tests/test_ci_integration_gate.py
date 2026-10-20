"""The CI runner must never promote a skipped real integration to PASS."""
from __future__ import annotations

from xml.etree import ElementTree as ET

import pytest

from scripts.ci_integration_gate import junit_counts, main


def _write_report(tmp_path, *, cases):
    suite = ET.Element("testsuites")
    children = ET.SubElement(suite, "testsuite")
    for outcome in cases:
        case = ET.SubElement(children, "testcase", name=outcome)
        if outcome != "passed":
            ET.SubElement(case, outcome, message=outcome)
    file = tmp_path / "report.xml"
    ET.ElementTree(suite).write(file)
    return file


def test_junit_counts_all_completed_cases(tmp_path):
    path = _write_report(tmp_path, cases=["passed", "passed", "skipped", "failure", "error"])
    assert junit_counts(path) == {"total": 5, "passed": 2, "skipped": 1, "failed": 2}


def test_junit_refuses_empty_suite(tmp_path):
    with pytest.raises(ValueError, match="no test cases"):
        junit_counts(_write_report(tmp_path, cases=[]))


def test_junit_refuses_unexpected_document(tmp_path):
    path = tmp_path / "invalid.xml"
    path.write_text("<not-junit/>", encoding="utf-8")
    with pytest.raises(ValueError, match="unexpected JUnit root"):
        junit_counts(path)


def test_gate_rejects_a_skipped_test(tmp_path):
    case = tmp_path / "test_skipped_case.py"
    case.write_text("import pytest\n\ndef test_unavailable():\n    pytest.skip('requires real service')\n", encoding="utf-8")
    assert main([str(case)]) == 1


def test_gate_accepts_executed_test(tmp_path):
    case = tmp_path / "test_passed_case.py"
    case.write_text("def test_real():\n    assert 2 + 2 == 4\n", encoding="utf-8")
    assert main([str(case)]) == 0
