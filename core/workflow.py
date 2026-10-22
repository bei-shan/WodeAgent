"""Deterministic fan-out of subagent work.

A Python counterpart to DeepSeek Harness' workflow orchestration: callers
describe tasks instead of writing a script, so runs are reproducible, bounded
and testable without a model in the loop.

Two shapes are supported:

``parallel``
    Every task runs concurrently under a bounded pool; one failure never
    cancels the others and results keep the caller's order.

``pipeline``
    Each item walks the stages in order; ``{item}`` and ``{prev}`` interpolate
    the original item and the previous stage's result. Items run concurrently,
    and a failed stage skips only the remaining stages of its own item.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Optional

MAX_CONCURRENCY_LIMIT = 16

Runner = Callable[[str, str], str]


class WorkflowError(Exception):
    """Invalid workflow definition or a failed stage."""


@dataclass
class TaskSpec:
    id: str
    prompt: str
    agent_type: str = "general"


@dataclass
class TaskOutcome:
    id: str
    status: str = "completed"  # completed | failed | skipped
    result: str = ""
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        payload = {"id": self.id, "status": self.status, "result": self.result}
        if self.error:
            payload["error"] = self.error
        return payload


@dataclass
class WorkflowResult:
    mode: str
    outcomes: list[TaskOutcome] = field(default_factory=list)
    events: list[dict[str, Any]] = field(default_factory=list)

    @property
    def completed(self) -> int:
        return sum(1 for outcome in self.outcomes if outcome.status == "completed")

    def to_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "completed": self.completed,
            "total": len(self.outcomes),
            "outcomes": [outcome.to_dict() for outcome in self.outcomes],
            "events": list(self.events),
        }


class Workflow:
    def __init__(
        self,
        runner: Runner,
        *,
        max_concurrency: int = 4,
        on_event: Optional[Callable[[str, dict[str, Any]], None]] = None,
        events_limit: int = 50,
    ):
        if not callable(runner):
            raise WorkflowError("runner must be callable")
        self._runner = runner
        self._max_concurrency = max(1, min(MAX_CONCURRENCY_LIMIT, int(max_concurrency)))
        self._on_event = on_event
        self._events_limit = max(0, int(events_limit))
        self._events: list[dict[str, Any]] = []

    # ------------------------------------------------------------------
    def _emit(self, kind: str, payload: dict[str, Any]) -> None:
        if len(self._events) < self._events_limit:
            self._events.append({"type": kind, **payload})
        if self._on_event is not None:
            self._on_event(kind, payload)

    def _run_one(self, spec: TaskSpec) -> TaskOutcome:
        try:
            text = self._runner(spec.prompt, spec.agent_type)
        except Exception as exc:  # one bad task must not sink the run
            return TaskOutcome(spec.id, "failed", error=str(exc) or type(exc).__name__)
        return TaskOutcome(spec.id, "completed", result=str(text or ""))

    # ------------------------------------------------------------------
    def run_parallel(self, tasks: Iterable[TaskSpec]) -> WorkflowResult:
        specs = _validate_tasks(tasks)
        result = WorkflowResult("parallel")
        self._emit("phase", {"title": "parallel"})
        with ThreadPoolExecutor(max_workers=self._max_concurrency) as pool:
            futures = [pool.submit(self._run_one, spec) for spec in specs]
            result.outcomes = [future.result() for future in futures]
        for outcome in result.outcomes:
            self._emit("task", {"id": outcome.id, "status": outcome.status})
        result.events = list(self._events)
        return result

    def run_pipeline(
        self,
        items: Iterable[str],
        stages: Iterable[TaskSpec],
    ) -> WorkflowResult:
        item_list = [_require_text(item, "item") for item in items]
        stage_list = _validate_tasks(stages, what="stage")
        if not item_list:
            raise WorkflowError("pipeline needs at least one item")
        result = WorkflowResult("pipeline")

        def run_item(index: int, item: str) -> TaskOutcome:
            previous = ""
            for stage in stage_list:
                prompt = _interpolate(stage.prompt, item=item, prev=previous)
                try:
                    previous = str(self._runner(prompt, stage.agent_type) or "")
                except Exception as exc:
                    return TaskOutcome(
                        f"{index}:{stage.id}", "failed",
                        error=str(exc) or type(exc).__name__,
                    )
                self._emit("stage", {"item": index, "stage": stage.id})
            return TaskOutcome(f"{index}:{stages_summary(stage_list)}", "completed", result=previous)

        self._emit("phase", {"title": "pipeline"})
        with ThreadPoolExecutor(max_workers=self._max_concurrency) as pool:
            futures = [pool.submit(run_item, index, item) for index, item in enumerate(item_list)]
            result.outcomes = [future.result() for future in futures]
        result.events = list(self._events)
        return result


def _validate_tasks(tasks: Iterable[TaskSpec], *, what: str = "task") -> list[TaskSpec]:
    specs: list[TaskSpec] = []
    seen: set[str] = set()
    for raw in tasks:
        spec = raw if isinstance(raw, TaskSpec) else _coerce_spec(raw)
        if not spec.id:
            raise WorkflowError(f"every {what} needs an id")
        if spec.id in seen:
            raise WorkflowError(f"duplicate {what} id: {spec.id}")
        seen.add(spec.id)
        specs.append(spec)
    if not specs:
        raise WorkflowError(f"at least one {what} is required")
    return specs


def _coerce_spec(raw: Any) -> TaskSpec:
    if not isinstance(raw, dict):
        raise WorkflowError("tasks must be objects with id and prompt")
    return TaskSpec(
        id=str(raw.get("id") or "").strip(),
        prompt=_require_text(raw.get("prompt"), "prompt"),
        agent_type=str(raw.get("agent_type") or "general"),
    )


def _require_text(value: Any, what: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise WorkflowError(f"{what} must be non-empty text")
    return text


def _interpolate(template: str, *, item: str, prev: str) -> str:
    try:
        return template.format(item=item, prev=prev)
    except KeyError as exc:
        raise WorkflowError(f"unknown placeholder in stage prompt: {exc}") from exc
    except (IndexError, ValueError) as exc:
        raise WorkflowError(f"invalid placeholder syntax in stage prompt: {exc}") from exc


def stages_summary(stages: list[TaskSpec]) -> str:
    return "+".join(stage.id for stage in stages)
