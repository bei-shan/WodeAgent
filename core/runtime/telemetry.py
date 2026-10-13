"""Privacy-first optional telemetry: native Langfuse v4 or OTLP/HTTP.

This module is deliberately separate from the local JSONL TraceLogger.
A disabled or failed exporter must never alter Agent execution.
"""
from __future__ import annotations

import atexit
import logging
import os
import re
from contextlib import contextmanager
from functools import lru_cache
from typing import Any, Iterator

logger = logging.getLogger(__name__)

# External exporters receive NO prompts, response content, paths, API keys,
# tool input/output or exception messages. This is an explicit allowlist.
_ALLOWED = frozenset({
    "ridge.run_id", "ridge.step", "ridge.tool.name", "ridge.tool.status",
    "ridge.turn.status", "ridge.model", "ridge.error.type",
    "gen_ai.usage.input_tokens", "gen_ai.usage.output_tokens",
    "gen_ai.usage.total_tokens",
})
_IDENT = re.compile(r"[^a-zA-Z0-9_:.\-]")


def _clean(value: Any) -> str | int | bool | None:
    if isinstance(value, bool):
        return value
    if type(value) is int:
        return max(0, min(value, 1_000_000_000))
    if isinstance(value, str):
        return _IDENT.sub("_", value)[:96]
    return None


class Observation:
    def __init__(self, raw: Any = None, *, backend: str = "off", kind: str = "span"):
        self.raw = raw
        self.backend = backend
        self.kind = kind

    def attribute(self, key: str, value: Any) -> None:
        if key not in _ALLOWED:
            return
        safe = _clean(value)
        if safe is None or self.raw is None:
            return
        try:
            if self.backend == "langfuse":
                self.raw.update(metadata={key: safe})
            else:
                self.raw.set_attribute(key, safe)
        except Exception:
            logger.warning("Telemetry attribute update failed", exc_info=False)

    def usage(self, usage: Any) -> None:
        if not isinstance(usage, dict) or self.raw is None:
            return
        items = {}
        for original, mapped in (
            ("prompt_tokens", "input"), ("completion_tokens", "output"),
            ("total_tokens", "total"),
        ):
            value = usage.get(original)
            if type(value) is int and value >= 0:
                items[mapped] = min(value, 1_000_000_000)
        if not items:
            return
        if self.backend == "langfuse" and self.kind == "generation":
            try:
                self.raw.update(usage_details=items)
            except Exception:
                logger.warning("Langfuse usage update failed", exc_info=False)
        else:
            for name, value in items.items():
                self.attribute(f"gen_ai.usage.{name}_tokens", value)


class Telemetry:
    def __init__(self, backend: str, target: Any):
        self.backend = backend
        self.target = target

    @contextmanager
    def span(self, name: str, *, kind: str = "span", model: str | None = None,
             attributes: dict[str, Any] | None = None) -> Iterator[Observation]:
        if self.backend == "off":
            yield Observation()
            return
        safe_name = _clean(name) or "agent.operation"
        try:
            if self.backend == "langfuse":
                options: dict[str, Any] = {
                    "name": safe_name,
                    "as_type": kind if kind in {"generation", "tool", "agent", "span"} else "span",
                }
                if kind == "generation" and model:
                    options["model"] = _clean(model)
                cm = self.target.start_as_current_observation(**options)
            else:
                cm = self.target.start_as_current_span(safe_name)
            raw = cm.__enter__()
        except Exception:
            logger.warning("Telemetry span start failed; executing without export", exc_info=False)
            yield Observation()
            return

        observation = Observation(raw, backend=self.backend, kind=kind)
        for key, value in (attributes or {}).items():
            observation.attribute(key, value)
        try:
            yield observation
        except BaseException:
            observation.attribute("ridge.error.type", "execution_error")
            try:
                # Never pass exception details into the SDK's context manager:
                # OTel otherwise auto-records the exception message/stack.
                cm.__exit__(None, None, None)
            except Exception:
                logger.warning("Telemetry span close failed", exc_info=False)
            raise
        else:
            try:
                cm.__exit__(None, None, None)
            except Exception:
                logger.warning("Telemetry span close failed", exc_info=False)


_DISABLED = Telemetry("off", None)


@lru_cache(maxsize=3)
def _initialize(backend: str, endpoint: str) -> Telemetry:
    try:
        if backend == "langfuse":
            if not (os.getenv("LANGFUSE_PUBLIC_KEY") and os.getenv("LANGFUSE_SECRET_KEY")):
                logger.warning("Langfuse telemetry requires public and secret keys")
                return _DISABLED
            from langfuse import get_client
            client = get_client()
            # The SDK owns an OTel batch processor. Do not flush synchronously
            # for every request on the serving thread.
            atexit.register(client.flush)
            return Telemetry("langfuse", client)
        if backend == "otlp":
            if not endpoint or not (
                endpoint.startswith("https://") or
                endpoint.startswith("http://127.0.0.1:") or
                endpoint.startswith("http://localhost:")
            ):
                logger.warning("OTLP traces endpoint must use HTTPS or local HTTP")
                return _DISABLED
            from opentelemetry.sdk.resources import Resource
            from opentelemetry.sdk.trace import TracerProvider
            from opentelemetry.sdk.trace.export import BatchSpanProcessor
            from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
            provider = TracerProvider(resource=Resource.create({"service.name": "ridge-code"}))
            exporter = OTLPSpanExporter(endpoint=endpoint, timeout=3)
            provider.add_span_processor(BatchSpanProcessor(
                exporter, max_queue_size=256, max_export_batch_size=32,
                schedule_delay_millis=1000, export_timeout_millis=3000,
            ))
            atexit.register(provider.shutdown)
            # Own a provider instead of changing the app-wide global tracer.
            return Telemetry("otlp", provider.get_tracer("ridge-code.agent"))
    except Exception:
        logger.warning("Optional telemetry SDK initialization failed; using local trace only", exc_info=False)
    return _DISABLED


def get_telemetry() -> Telemetry:
    backend = os.getenv("AGENT_TELEMETRY_BACKEND", "off").strip().lower()
    if backend == "off":
        return _DISABLED
    if backend not in ("langfuse", "otlp"):
        logger.warning("Unknown AGENT_TELEMETRY_BACKEND; tracing disabled")
        return _DISABLED
    if backend == "langfuse" and not (
        os.getenv("LANGFUSE_PUBLIC_KEY") and os.getenv("LANGFUSE_SECRET_KEY")
    ):
        logger.warning("Langfuse telemetry requires public and secret keys")
        return _DISABLED
    endpoint = os.getenv("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", "") if backend == "otlp" else ""
    return _initialize(backend, endpoint)
