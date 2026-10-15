"""Deterministic, provider-free Agent task evaluation for CI.

Run: python scripts/agent_eval.py --json

This evaluates concrete file side effects, tool interactions, safe denial,
continuation and context-compaction invariants. It is **not** a real-LLM
quality benchmark, and its JSON report never contains prompts or tool output.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def tool(name: str, arguments: dict, ident: int) -> dict:
    return {
        "id": f"eval_{ident}", "type": "function",
        "function": {"name": name, "arguments": json.dumps(arguments)},
    }


def answer(content: str = "", calls: list[dict] | None = None) -> dict:
    return {"choices": [{"message": {"content": content, "tool_calls": calls or []}}]}


class ScriptedProvider:
    """Explicit local deterministic responses; no provider/network API key."""
    def __init__(self, script: tuple[dict, ...]):
        self.script = script
        self.calls = 0

    def invoke_raw(self, messages, tools=None, tool_choice=None):
        if self.calls >= len(self.script):
            raise RuntimeError("eval_script_exhausted")
        value = self.script[self.calls]
        self.calls += 1
        return value


@dataclass(frozen=True)
class Case:
    id: str
    initial: dict[str, str]
    expected: dict[str, str]
    preserved: tuple[str, ...]
    prompts: tuple[str, ...]
    script: tuple[dict, ...]


CASES: tuple[Case, ...] = (
    Case(
        id="cross_file_refactor",
        initial={"src/settings.py": "MODE = 'dev'\n",
                 "docs/usage.md": "Mode: dev\n"},
        expected={"src/settings.py": "MODE = 'prod'\n",
                  "docs/usage.md": "Mode: prod\n"},
        preserved=(),
        prompts=("Align both project config and docs.",),
        script=(
            answer(calls=[
                tool("Read", {"path": "src/settings.py"}, 1),
                tool("Read", {"path": "docs/usage.md"}, 2),
            ]),
            answer(calls=[
                tool("Edit", {"path": "src/settings.py",
                              "old_string": "MODE = 'dev'",
                              "new_string": "MODE = 'prod'"}, 3),
                tool("Edit", {"path": "docs/usage.md",
                              "old_string": "Mode: dev",
                              "new_string": "Mode: prod"}, 4),
            ]),
            answer("Both files updated."),
        ),
    ),
    Case(
        id="tool_error_recovery",
        initial={"notes.txt": "safe and unchanged\n"},
        expected={"report.txt": "Handled missing file\n"},
        preserved=("notes.txt",),
        prompts=("Handle the missing input without changing notes.",),
        script=(
            answer(calls=[tool("Read", {"path": "missing.txt"}, 1)]),
            answer(calls=[tool("Write", {"path": "report.txt",
                                        "content": "Handled missing file\n"}, 2)]),
            answer("Recorded the missing input."),
        ),
    ),
    Case(
        id="multiturn_continuation",
        initial={},
        expected={"plan.txt": "review first\n"},
        preserved=(),
        prompts=("Create a safe plan file.", "Check the existing plan."),
        script=(
            answer(calls=[tool("Write", {"path": "plan.txt",
                                        "content": "review first\n"}, 1)]),
            answer("Plan created."),
            answer(calls=[tool("Read", {"path": "plan.txt"}, 2)]),
            answer("The plan still says review first."),
        ),
    ),
    Case(
        id="outside_workspace_denial",
        initial={"keep.txt": "no edits\n"},
        expected={},
        preserved=("keep.txt",),
        prompts=("Attempt a project-external change, then report denial.",),
        script=(
            answer(calls=[tool("Write", {"path": "../private.txt",
                                        "content": "must-not-write"}, 1)]),
            answer("External writes were denied."),
        ),
    ),
)


@contextmanager
def _isolated_cwd() -> Iterator[Path]:
    original = Path.cwd()
    with tempfile.TemporaryDirectory(prefix="ridge-eval-") as directory:
        try:
            os.chdir(directory)
            root = Path(directory) / "project"
            root.mkdir()
            yield root
        finally:
            os.chdir(original)


def _run_case(case: Case) -> dict:
    from agents.codeAgent import CodeAgent
    from core.config import Config
    from tools.registry import ToolRegistry
    from tools.builtin.read_file import ReadTool
    from tools.builtin.write_file import WriteTool
    from tools.builtin.edit_file import EditTool

    start = time.monotonic()
    passed = False
    errors: list[str] = []
    calls = 0
    observed_turns = 0
    with _isolated_cwd() as root:
        for path, content in case.initial.items():
            destination = root / path
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_text(content, encoding="utf-8")
        # Outside-root sentinel doubles as the attempted forbidden destination.
        forbidden = root.parent / "private.txt"
        forbidden.write_text("protected\n", encoding="utf-8")
        initial_snapshot = {
            path: (root / path).read_bytes() for path in case.preserved
        }
        mock = ScriptedProvider(case.script)
        registry = ToolRegistry()
        for cls in (ReadTool, WriteTool, EditTool):
            registry.register_tool(cls(project_root=root))
        agent = None
        try:
            agent = CodeAgent(
                name="offline-eval", llm=mock, tool_registry=registry,
                project_root=str(root),
                config=Config(
                    sandbox_mode="local_trusted", agent_interactive=False,
                    llm_streaming=False, trace_enabled=False,
                    enable_agent_teams=False, max_steps=8,
                    show_react_steps=False, show_progress=False,
                ),
            )
            for prompt in case.prompts:
                output = agent.run(prompt)
                observed_turns += 1
                if not isinstance(output, str) or not output.strip():
                    errors.append("missing_final")
                if getattr(agent, "last_turn_status", None) != "completed":
                    errors.append("turn_not_completed")
        except Exception:
            errors.append("agent_execution_failed")
        finally:
            calls = mock.calls
            if agent is not None:
                try:
                    agent.close()
                except Exception:
                    errors.append("agent_cleanup_failed")
        if mock.calls != len(case.script):
            errors.append("unexpected_model_calls")
        for path, expected in case.expected.items():
            actual = root / path
            if not actual.is_file() or actual.read_text(encoding="utf-8") != expected:
                errors.append("expected_artifact_mismatch")
        for path, original in initial_snapshot.items():
            actual = root / path
            if not actual.is_file() or actual.read_bytes() != original:
                errors.append("preserved_file_changed")
        if forbidden.read_bytes() != b"protected\n":
            errors.append("escaped_workspace")
        if any(e == "outside_workspace_denial" for e in (case.id,)):
            # A denial must not be silently followed by a new file in the parent.
            if forbidden.read_bytes() != b"protected\n":
                errors.append("deny_policy_failed")
        passed = not errors
    return {
        "id": case.id,
        "pass": passed,
        "model_calls": calls,
        "turns": observed_turns,
        "duration_ms": int((time.monotonic() - start) * 1000),
        "errors": sorted(set(errors)),
    }


def _context_case() -> dict:
    from core.config import Config
    from core.context_engine.history_manager import HistoryManager

    start = time.monotonic()
    history = HistoryManager(
        config=Config(min_retain_rounds=2),
        summary_generator=lambda _: "Completed earlier steps, tests pending.",
    )
    history.append_user("Goal: keep auth read-only")
    history.append_assistant("Continue")
    for i in range(6):
        history.append_user(f"step {i}")
        history.append_assistant(f"done {i}")
    compressed = history.compact()
    snapshot = history.serialize_entries()
    restored = HistoryManager(config=Config(min_retain_rounds=2))
    restored.load_entries(snapshot)
    summaries = [m.content for m in restored.get_messages() if m.role == "summary"]
    ok = bool(compressed and len(summaries) == 1
              and "Goal: keep auth read-only" in summaries[0]
              and restored.get_cursor_id() == history.get_cursor_id())
    return {
        "id": "context_compaction_continuity", "pass": ok,
        "model_calls": 0, "turns": 0,
        "duration_ms": int((time.monotonic() - start) * 1000),
        "errors": [] if ok else ["context_invariant_failed"],
    }


def run_eval(ids: tuple[str, ...] | None = None) -> dict:
    allowed = {c.id for c in CASES} | {"context_compaction_continuity"}
    selected = set(ids) if ids is not None else allowed
    if not selected or selected - allowed:
        raise ValueError("Unknown evaluation case")
    results = [_run_case(case) for case in CASES if case.id in selected]
    if "context_compaction_continuity" in selected:
        results.append(_context_case())
    return {
        "schema_version": 1,
        "mode": "offline_scripted_no_provider",
        "result": "pass" if all(x["pass"] for x in results) else "fail",
        "case_count": len(results),
        "passed": sum(bool(x["pass"]) for x in results),
        "model_calls": sum(x["model_calls"] for x in results),
        "cases": results,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="Print sanitized JSON report")
    parser.add_argument("--case", action="append", dest="cases", default=None)
    args = parser.parse_args(argv)
    try:
        report = run_eval(tuple(args.cases) if args.cases else None)
    except ValueError as exc:
        parser.error(str(exc))
    if args.json:
        print(json.dumps(report, sort_keys=True))
    else:
        print(f"Agent deterministic eval: {report['result']} "
              f"({report['passed']}/{report['case_count']})")
        for case in report["cases"]:
            print(f" - {case['id']}: {'pass' if case['pass'] else 'fail'}")
    return 0 if report["result"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
