"""Conservative batching for multiple tool calls from one model step.

Only explicitly safe consecutive calls may overlap. Unsafe calls are barriers,
and results are returned in model order. The Agent owns history/events/hooks;
this module owns grouping and bounded execution only.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any, Callable, Sequence, TypeVar

T = TypeVar("T")
R = TypeVar("R")


@dataclass(frozen=True)
class ToolBatch:
    calls: tuple[Any, ...]
    concurrent: bool


def plan_tool_batches(
    calls: Sequence[T],
    is_concurrency_safe: Callable[[T], bool],
    *,
    max_workers: int = 4,
) -> list[ToolBatch]:
    """Group adjacent safe calls; each unsafe call becomes a serial barrier."""
    batches: list[ToolBatch] = []
    safe_run: list[T] = []
    enabled = max_workers >= 2

    def flush() -> None:
        if safe_run:
            batches.append(ToolBatch(tuple(safe_run), concurrent=enabled and len(safe_run) > 1))
            safe_run.clear()

    for call in calls:
        safe = False
        if enabled:
            try:
                safe = bool(is_concurrency_safe(call))
            except Exception:
                # Failure to establish safety must never opt a tool into concurrency.
                safe = False
        if safe:
            safe_run.append(call)
            continue
        flush()
        batches.append(ToolBatch((call,), concurrent=False))
    flush()
    return batches


def run_tool_batch(
    batch: ToolBatch,
    execute: Callable[[T], R],
    *,
    max_workers: int = 4,
) -> list[R]:
    """Execute one batch, always collecting results in input order.

    Callbacks for events, history and UI should stay on the calling thread.
    Only the pure tool-execution callback is submitted to the pool.
    """
    if not batch.concurrent or max_workers < 2:
        return [execute(item) for item in batch.calls]
    with ThreadPoolExecutor(
        max_workers=min(max_workers, len(batch.calls)),
        thread_name_prefix="agent-tool",
    ) as pool:
        return list(pool.map(execute, batch.calls))
