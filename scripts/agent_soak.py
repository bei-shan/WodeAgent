"""Repeatable local Agent lifecycle soak, with bounded resource reporting.

Offline simulation: exercises *real* SessionController worker threads,
EventJournal replay and session cleanup, using a fake provider. It does not
claim end-to-end real-model/MCP/OAuth acceptance.

  python scripts/agent_soak.py --duration-seconds 1800 --json

Exit code 1 on leaked workers, failed turns, excessive FD/thread/RSS growth
or failed journal replay; exit code 2 on invalid arguments.
"""
from __future__ import annotations

import argparse
import gc
import json
import os
import sys
import tempfile
import threading
import time
from contextlib import contextmanager
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def snapshot() -> dict[str, int | None]:
    """Steady-state host process metrics; no secrets or tool inputs."""
    fd_dir = Path("/proc/self/fd")
    fds = len(list(fd_dir.iterdir())) if fd_dir.is_dir() else None
    rss = None
    status = Path("/proc/self/status")
    if status.is_file():
        try:
            for line in status.read_text().splitlines():
                if line.startswith("VmRSS:"):
                    rss = int(line.split()[1]) * 1024
                    break
        except (ValueError, OSError):
            pass
    return {"threads": threading.active_count(), "fds": fds, "rss_bytes": rss}


@contextmanager
def isolated_workspace():
    """Never pollute the operator's sessions or data directory."""
    old = Path.cwd()
    with tempfile.TemporaryDirectory(prefix="ridge-soak-") as home:
        try:
            os.chdir(home)
            yield Path(home)
        finally:
            os.chdir(old)


def run_soak(
    *, duration_seconds: float = 1800.0, iterations: int = 0,
    max_fd_growth: int = 12, max_thread_growth: int = 5,
    max_rss_growth_mib: float = 96.0,
) -> dict:
    if duration_seconds < 0 or iterations < 0 or (
        duration_seconds == 0 and iterations == 0
    ):
        raise ValueError("Specify a positive duration or iteration count")
    if any(x < 0 for x in (max_fd_growth, max_thread_growth, max_rss_growth_mib)):
        raise ValueError("Resource growth limits must not be negative")

    from core.runtime.session_controller import SessionController
    from tests.test_web_service import FakeAgent

    started = time.monotonic()
    baseline = snapshot()
    last = baseline
    peak_fd = baseline["fds"]
    peak_threads = baseline["threads"]
    peak_rss = baseline["rss_bytes"]
    cycles = 0
    errors = []
    with isolated_workspace() as root:
        controller = SessionController(workspace_base=str(root / ".mycodeagent/sessions"))
        try:
            while ((iterations > 0 and cycles < iterations) or
                   (iterations == 0 and time.monotonic() - started < duration_seconds)):
                sid = controller.create_session(FakeAgent)
                cycles += 1
                session = controller.get_session(sid)
                try:
                    if not session.send_message(f"soak turn {cycles}"):
                        errors.append("turn_admission_failed")
                        continue
                    thread = session._thread
                    if thread is not None:
                        thread.join(timeout=6)
                    if session.busy or session.last_turn.status.value != "completed":
                        errors.append("turn_failed_or_hung")
                        try:
                            session.interrupt()
                        except Exception:
                            pass
                        continue
                    latest = session.events.latest_seq
                    # Two independent consumers can replay identical events.
                    first = session.events.read_after(0)
                    second = session.events.read_after(0)
                    if (latest < 2 or first.events != second.events
                            or not first.events
                            or first.events[-1].event.type != "turn.completed"):
                        errors.append("event_replay_failure")
                finally:
                    if not controller.delete_session(sid):
                        errors.append("session_close_failed")
                if cycles % 10 == 0:
                    last = snapshot()
                    peak_threads = max(peak_threads, last["threads"])
                    if last["fds"] is not None:
                        peak_fd = max(peak_fd or 0, last["fds"])
                    if last["rss_bytes"] is not None:
                        peak_rss = max(peak_rss or 0, last["rss_bytes"])
        finally:
            closed = controller.shutdown(grace_seconds=2)
            if closed["running"] or controller.list_sessions():
                errors.append("sessions_left_running")
    gc.collect()
    time.sleep(0.01)
    last = snapshot()
    rss_growth = (
        (last["rss_bytes"] - baseline["rss_bytes"]) / (1024 ** 2)
        if last["rss_bytes"] is not None and baseline["rss_bytes"] is not None
        else None
    )
    fd_growth = (
        last["fds"] - baseline["fds"]
        if last["fds"] is not None and baseline["fds"] is not None else None
    )
    thread_growth = last["threads"] - baseline["threads"]
    if fd_growth is not None and fd_growth > max_fd_growth:
        errors.append("fd_growth")
    if thread_growth > max_thread_growth:
        errors.append("thread_growth")
    if rss_growth is not None and rss_growth > max_rss_growth_mib:
        errors.append("rss_growth")
    return {
        "result": "pass" if not errors else "fail",
        "mode": "offline_fake_provider",
        "duration_seconds": round(time.monotonic() - started, 2),
        "cycles": cycles,
        "start": baseline, "end": last,
        "peak": {"fds": peak_fd, "threads": peak_threads, "rss_bytes": peak_rss},
        "growth": {"fds": fd_growth, "threads": thread_growth,
                   "rss_mib": round(rss_growth, 2) if rss_growth is not None else None},
        "errors": errors[:20],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--duration-seconds", type=float, default=1800)
    parser.add_argument("--iterations", type=int, default=0)
    parser.add_argument("--max-fd-growth", type=int, default=12)
    parser.add_argument("--max-thread-growth", type=int, default=5)
    parser.add_argument("--max-rss-growth-mib", type=float, default=96)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    try:
        report = run_soak(
            duration_seconds=args.duration_seconds, iterations=args.iterations,
            max_fd_growth=args.max_fd_growth,
            max_thread_growth=args.max_thread_growth,
            max_rss_growth_mib=args.max_rss_growth_mib,
        )
    except ValueError as exc:
        parser.error(str(exc))
    if args.json:
        print(json.dumps(report, ensure_ascii=False))
    else:
        print(f"Agent offline soak: {report['result']} — "
              f"{report['cycles']} cycles in {report['duration_seconds']} s")
        print("Resource growth:", report["growth"])
        if report["errors"]:
            print("Failures:", ", ".join(report["errors"]))
    return 0 if report["result"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
