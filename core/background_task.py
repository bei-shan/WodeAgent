"""Background task runner for async subagent execution.

Sub-agents launched with ``run_in_background=True`` execute in daemon
threads.  Results are persisted to ``.tasks/output/{task_id}.json``
with atomic writes (tmp → rename).  Step-by-step progress is written
to ``.tasks/progress/{task_id}.jsonl`` for real-time streaming.

The main agent polls completed results via TaskOutputTool and sees
running tasks via runtime system blocks.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


class BackgroundTaskRunner:
    """Launches and tracks background sub-agent tasks.

    Parameters
    ----------
    output_dir:
        Directory for task result files.  Default: ``.tasks/output/``
        relative to *project_root*.
    """

    def __init__(self, project_root: Path, output_dir: str | None = None):
        self._root = Path(project_root).resolve()
        dir_name = output_dir or os.getenv("BG_TASK_OUTPUT_DIR", ".tasks/output")
        self._output_dir = self._root / dir_name
        self._output_dir.mkdir(parents=True, exist_ok=True)
        self._progress_dir = self._root / ".tasks/progress"
        self._progress_dir.mkdir(parents=True, exist_ok=True)
        self._tasks: Dict[str, dict] = {}
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def launch(
        self,
        task_id: str,
        runner_callable,  # Callable[[callable | None], tuple[str, dict]]
        description: str = "",
    ) -> None:
        """Launch a background task in a daemon thread.

        Parameters
        ----------
        task_id:
            Unique task identifier.
        runner_callable:
            A callable ``(progress_callback) -> (result: str, tool_usage: dict)``
            that executes the sub-agent synchronously.  The callback receives
            ``(step, event_type, data_dict)`` for each ReAct step.
            Its return value is persisted on success.
        description:
            Short description shown in runtime summary.
        """
        with self._lock:
            if task_id in self._tasks:
                logger.warning("Duplicate background task id: %s", task_id)
                return
            self._tasks[task_id] = {
                "status": "running",
                "description": str(description or "")[:200],
                "started_at": time.time(),
            }

        progress_path = self._progress_dir / f"{task_id}.jsonl"
        # Clear any stale progress file.
        try:
            progress_path.unlink(missing_ok=True)
        except OSError:
            pass

        def _progress_callback(step: int, event_type: str, data: dict) -> None:
            """Write a progress entry to the JSONL file."""
            try:
                entry = json.dumps(
                    {"step": step, "type": event_type, **data},
                    ensure_ascii=False,
                )
                with open(progress_path, "a", encoding="utf-8") as f:
                    f.write(entry + "\n")
            except Exception:
                pass

        def _run() -> None:
            try:
                # Try with progress callback first, fall back to no args.
                import inspect
                try:
                    sig = inspect.signature(runner_callable)
                    if len(sig.parameters) > 0:
                        result, tool_usage = runner_callable(_progress_callback)
                    else:
                        result, tool_usage = runner_callable()
                except (ValueError, TypeError):
                    result, tool_usage = runner_callable()
                self._save_result(task_id, "completed", result, tool_usage)
            except Exception as exc:
                self._save_result(task_id, "failed", str(exc), {})
            finally:
                # Write a final "done" marker.
                try:
                    with open(progress_path, "a", encoding="utf-8") as f:
                        f.write(json.dumps({"type": "done"}) + "\n")
                except Exception:
                    pass

        thread = threading.Thread(target=_run, name=f"BgTask[{task_id[:8]}]", daemon=True)
        thread.start()
        logger.info("Background task %s started: %s", task_id, description)

    def get_result(self, task_id: str) -> dict | None:
        """Return the persisted result for *task_id*, or None if not finished.

        Reads ``.tasks/output/{task_id}.json``.
        """
        result_path = self._output_dir / f"{task_id}.json"
        if not result_path.exists():
            return None
        try:
            return json.loads(result_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return None

    def get_status(self, task_id: str) -> str:
        """Return the current status: 'running' | 'completed' | 'failed' | 'not_found'."""
        result = self.get_result(task_id)
        if result is not None:
            return str(result.get("status", "unknown"))
        with self._lock:
            task = self._tasks.get(task_id)
            if task:
                return str(task.get("status", "running"))
        return "not_found"

    def list_all(self) -> List[dict]:
        """Return all tracked tasks with current status and metadata."""
        tasks: List[dict] = []
        with self._lock:
            for tid, meta in self._tasks.items():
                info = dict(meta)
                info["task_id"] = tid
                info["elapsed"] = time.time() - meta.get("started_at", time.time())
                tasks.append(info)
        tasks.sort(key=lambda t: t.get("started_at", 0))
        return tasks

    def summary_text(self) -> str:
        """Produce a single-line-per-task summary for runtime blocks.

        Format::

            [Background Tasks]
            - abc123: ✓ completed (12s)
            - def456: ⏳ running (8s) — analyzing auth...
            - ghi789: ✗ failed: connection timeout
        """
        tasks = self.list_all()
        if not tasks:
            return ""
        lines = ["[Background Tasks]"]
        for t in tasks:
            tid = t["task_id"]
            status = t["status"]
            elapsed = t.get("elapsed", 0)
            if status == "completed":
                lines.append(f"- {tid}: ✓ completed ({elapsed:.0f}s)")
            elif status == "failed":
                error = str(t.get("error", ""))[:80]
                lines.append(f"- {tid}: ✗ failed: {error}")
            else:
                desc = str(t.get("description", ""))[:60]
                lines.append(f"- {tid}: ⏳ running ({elapsed:.0f}s) — {desc}")
        return "\n".join(lines)

    def clear_completed(self) -> int:
        """Remove completed/failed tasks from memory tracking.  Returns count."""
        count = 0
        with self._lock:
            for tid in list(self._tasks.keys()):
                if self._tasks[tid]["status"] in ("completed", "failed"):
                    del self._tasks[tid]
                    count += 1
        return count

    def get_progress(self, task_id: str, since_step: int = 0) -> list[dict]:
        """Read progress entries for *task_id* from the JSONL file.

        Returns entries with step > *since_step*.  Useful for polling.
        """
        progress_path = self._progress_dir / f"{task_id}.jsonl"
        if not progress_path.exists():
            return []
        entries: list[dict] = []
        try:
            for line in progress_path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if entry.get("type") == "done":
                    break
                step = entry.get("step", 0)
                if step > since_step:
                    entries.append(entry)
        except OSError:
            pass
        return entries

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _save_result(self, task_id: str, status: str, result: str, tool_usage: dict) -> None:
        """Persist task result with atomic write."""
        output = {
            "task_id": task_id,
            "status": status,
            "result": str(result or ""),
            "tool_usage": tool_usage or {},
            "finished_at": time.time(),
        }
        # Atomic write: tmp → rename
        tmp_path = self._output_dir / f"{task_id}.tmp"
        final_path = self._output_dir / f"{task_id}.json"
        try:
            tmp_path.write_text(json.dumps(output, ensure_ascii=False), encoding="utf-8")
            tmp_path.replace(final_path)
        except OSError as exc:
            logger.error("Failed to save background task %s: %s", task_id, exc)
            output["status"] = "failed"
            output["error"] = str(exc)

        with self._lock:
            if task_id in self._tasks:
                self._tasks[task_id]["status"] = status
                self._tasks[task_id]["finished_at"] = output.get("finished_at")
                if status == "failed":
                    self._tasks[task_id]["error"] = str(result or "")
        logger.info("Background task %s: %s", task_id, status)
