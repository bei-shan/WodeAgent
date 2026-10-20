"""Run selected real integration tests and fail closed on any skip.

The ordinary full regression suite intentionally works without network
services. This entry point is only for jobs that *claim* a real dependency.
Each chosen test must execute; green CI cannot mean "all skipped".
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET


def junit_counts(report: Path) -> dict[str, int]:
    """Count individual pytest JUnit cases; reject an empty/missing report."""
    root = ET.parse(report).getroot()
    if root.tag not in {"testsuite", "testsuites"}:
        raise ValueError("unexpected JUnit root")
    cases = list(root.iter("testcase"))
    if not cases:
        raise ValueError("no test cases collected")
    return {
        "total": len(cases),
        "passed": sum(not any(case.find(k) is not None for k in ("skipped", "failure", "error"))
                      for case in cases),
        "skipped": sum(case.find("skipped") is not None for case in cases),
        "failed": sum(case.find("failure") is not None or case.find("error") is not None
                      for case in cases),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("tests", nargs="+", help="Existing integration test file(s)")
    args = parser.parse_args(argv)
    with tempfile.TemporaryDirectory(prefix="ridge-integration-") as workdir:
        report = Path(workdir) / "pytest-junit.xml"
        command = [
            sys.executable, "-m", "pytest", *args.tests, "-q", "-ra",
            f"--junitxml={report}",
        ]
        status = subprocess.run(command, check=False).returncode
        try:
            counts = junit_counts(report)
        except (OSError, ET.ParseError, ValueError) as exc:
            print(f"::error::Missing or invalid test evidence: {exc}", flush=True)
            return 1
    print("Real integration evidence: " + ", ".join(
        f"{key}={value}" for key, value in counts.items()
    ), flush=True)
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as output:
            output.write(
                f"### Real integration gate: {', '.join(args.tests)}\n"
                f"- Executed: {counts['total']}\n"
                f"- Passed: {counts['passed']}\n"
                f"- Skipped: {counts['skipped']}\n"
                f"- Failed: {counts['failed']}\n"
            )
    if status != 0 or counts["skipped"] or counts["failed"]:
        print("::error::Real integration gate failed: tests must run, with no skips or failures", flush=True)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
