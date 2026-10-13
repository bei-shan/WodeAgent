"""Read-only, bounded metadata projection of private local agent JSONL traces.

The private JSONL audit contains prompts, outputs and arguments. Nothing from
its raw payload may be returned to the browser without explicit allowlisting.
"""
from __future__ import annotations

import json
import math
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Any

_MAX_BYTES = 4 * 1024 * 1024
_MAX_EVENTS = 5000
_MAX_RUNS = 8
_MAX_NODES = 240
_IDENTIFIER = re.compile(r"[^a-zA-Z0-9_.:\-]")


def _name(raw: Any, default: str = "unknown") -> str:
    if not isinstance(raw, str):
        return default
    value = _IDENTIFIER.sub("_", raw)[:72]
    return value or default


def _positive_int(raw: Any) -> int:
    return min(raw, 1_000_000_000) if type(raw) is int and raw >= 0 else 0


def _timestamp(raw: Any) -> str | None:
    if not isinstance(raw, str):
        return None
    try:
        dt = datetime.fromisoformat(raw)
    except (ValueError, TypeError):
        return None
    return dt.isoformat()


def _elapsed(start: str | None, end: str | None) -> int | None:
    if not start or not end:
        return None
    try:
        delta = (datetime.fromisoformat(end) - datetime.fromisoformat(start)).total_seconds()
        return int(delta * 1000) if 0 <= delta <= 86400 else None
    except (ValueError, TypeError):
        return None


def _usage(raw: Any) -> dict[str, int]:
    data = raw if isinstance(raw, dict) else {}
    return {
        "input": _positive_int(data.get("prompt_tokens")),
        "output": _positive_int(data.get("completion_tokens")),
        "total": _positive_int(data.get("total_tokens")),
    }


def _rate(key: str) -> float | None:
    text = os.getenv(key, "")
    try:
        price = float(text)
    except (ValueError, TypeError):
        return None
    if not math.isfinite(price) or not 0 <= price <= 1000:
        return None
    return price


def _status(value: Any) -> str:
    return value if value in ("success", "partial", "error", "completed", "failed", "cancelled", "running") else "unknown"


def _new_run(run_id: int, started_at: str | None) -> dict[str, Any]:
    return {
        "run_id": run_id, "status": "running", "started_at": started_at,
        "duration_ms": None, "tokens": {"input": 0, "output": 0, "total": 0},
        "cost_usd_estimate": None,
        "quality": {"label": "pending", "tool_errors": 0, "empty_retries": 0,
                    "note": "运行健康度，不代表答案准确性"},
        "steps": [],
    }


def analyze_trace(path: Path, trace_id: str) -> dict[str, Any]:
    """Project local metadata only, never raw strings from inputs or outputs."""
    empty = {"available": False, "trace_id": _name(trace_id),
             "truncated": False, "pricing_configured": False, "runs": []}
    if not path.is_file():
        return empty
    if path.is_symlink():
        return empty

    input_price = _rate("TRACE_INPUT_USD_PER_MILLION")
    output_price = _rate("TRACE_OUTPUT_USD_PER_MILLION")
    priced = input_price is not None and output_price is not None
    size = path.stat().st_size
    truncated = size > _MAX_BYTES

    runs: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    step_map: dict[int, dict[str, Any]] = {}
    tool_nodes: dict[str, dict[str, Any]] = {}
    saw_model_call = False
    event_count = 0
    with path.open("rb") as stream:
        if truncated:
            stream.seek(size - _MAX_BYTES)
            stream.readline()  # discard incomplete line from the tail window
        for raw_line in stream:
            event_count += 1
            if event_count > _MAX_EVENTS:
                truncated = True
                break
            if len(raw_line) > 131072:
                truncated = True
                continue
            try:
                event = json.loads(raw_line)
            except (ValueError, UnicodeDecodeError):
                continue
            if not isinstance(event, dict):
                continue
            kind = event.get("event")
            payload = event.get("payload")
            if not isinstance(payload, dict):
                payload = {}
            step_no = _positive_int(event.get("step"))
            ts = _timestamp(event.get("ts"))

            if kind == "run_start":
                rid = _positive_int(payload.get("run_id"))
                if rid == 0:
                    continue
                current = _new_run(rid, ts)
                runs.append(current)
                step_map, tool_nodes = {}, {}
                saw_model_call = False
                continue
            if current is None:
                continue
            if kind == "run_end":
                if _positive_int(payload.get("run_id")) == current["run_id"]:
                    current["status"] = _status(payload.get("status"))
                    current["duration_ms"] = _elapsed(current["started_at"], ts)
                continue
            if kind == "empty_response_retry":
                current["quality"]["empty_retries"] += 1
                continue
            if kind not in {"model_call", "model_output", "tool_call", "tool_result", "error"}:
                continue
            if step_no <= 0 or step_no > 1000:
                continue
            if step_no not in step_map:
                step_map[step_no] = {"step": step_no, "nodes": []}
                current["steps"].append(step_map[step_no])
            nodes = step_map[step_no]["nodes"]
            if len(nodes) >= _MAX_NODES:
                truncated = True
                continue
            if kind == "model_call":
                saw_model_call = True
                usage = _usage(payload.get("usage"))
                nodes.append({
                    "kind": "model", "name": _name(payload.get("model")),
                    "status": _status(payload.get("status")),
                    "duration_ms": _positive_int(payload.get("duration_ms")),
                    "tokens": usage,
                })
                for key in ("input", "output", "total"):
                    current["tokens"][key] += usage[key]
            elif kind == "model_output" and not saw_model_call:
                usage = _usage(payload.get("usage"))
                nodes.append({"kind": "model", "name": "model",
                              "status": "success", "duration_ms": None,
                              "tokens": usage})
                for key in ("input", "output", "total"):
                    current["tokens"][key] += usage[key]
            elif kind == "tool_call":
                node = {
                    "kind": "tool", "name": _name(payload.get("tool")),
                    "status": "running", "duration_ms": None,
                }
                nodes.append(node)
                call_id = payload.get("tool_call_id")
                if isinstance(call_id, str):
                    tool_nodes[call_id] = node  # internal lookup only; not returned
            elif kind == "tool_result":
                call_id = payload.get("tool_call_id")
                node = tool_nodes.get(call_id) if isinstance(call_id, str) else None
                if node:
                    result = payload.get("result")
                    node["status"] = _status(result.get("status")) if isinstance(result, dict) else "unknown"
                    node["duration_ms"] = _positive_int(payload.get("duration_ms"))
                    if node["status"] == "error":
                        current["quality"]["tool_errors"] += 1
            elif kind == "error" and payload.get("stage") in ("tool_execution", "tool_call_parse"):
                call_id = payload.get("tool_call_id")
                node = tool_nodes.get(call_id) if isinstance(call_id, str) else None
                if node and node["status"] == "running":
                    node["status"] = "error"
                    current["quality"]["tool_errors"] += 1

    for run in runs:
        run["steps"].sort(key=lambda step: step["step"])
        if priced:
            usage = run["tokens"]
            run["cost_usd_estimate"] = round(
                (usage["input"] * input_price + usage["output"] * output_price) / 1_000_000, 8
            )
        errs = run["quality"]["tool_errors"]
        retries = run["quality"]["empty_retries"]
        run["quality"]["label"] = (
            "needs_review" if run["status"] in ("failed", "cancelled") or errs > 0
            else "warning" if retries > 0 or run["status"] != "completed"
            else "healthy"
        )
    return {
        "available": True, "trace_id": _name(trace_id),
        "truncated": truncated, "pricing_configured": priced,
        "runs": runs[-_MAX_RUNS:][::-1],
    }
