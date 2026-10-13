"""Model provider dispatch + ordered feature intercept chain.

AgentLoop decides when to request a model invocation. CodeAgent retains
recovery, usage accounting, event emission, history, and terminal outcomes.
ModelGateway owns the streaming choice and LLM interception mechanism.
"""
from __future__ import annotations

from typing import Any, Callable, Iterable, Protocol


class ModelProvider(Protocol):
    def invoke_raw(self, messages: list[dict], *, tools: Any, tool_choice: str) -> Any: ...
    # stream_raw is optional for legacy providers, checked via hasattr.


class ModelInterceptor(Protocol):
    def llm_intercept(
        self, agent: Any, messages: list[dict], tools_schema: Any,
        tool_choice: str, invoke_next: Callable[[], Any],
    ) -> Any: ...


class ModelGateway:
    """One model invocation with deterministic interceptor nesting."""

    def __init__(self, provider: ModelProvider, *, streaming: bool = True):
        self.provider = provider
        self.streaming = streaming

    def invoke(
        self, *, messages: list[dict], tools_schema: Any,
        tool_choice: str, features: Iterable[ModelInterceptor],
        agent: Any, on_delta: Callable[[str, str], None],
    ) -> Any:
        from core.runtime.telemetry import get_telemetry
        from core.response_parser import extract_usage

        model_name = getattr(self.provider, "model", None)
        with get_telemetry().span(
            "agent.model", kind="generation", model=model_name,
        ) as observation:
            result = self._invoke_untraced(
                messages=messages, tools_schema=tools_schema,
                tool_choice=tool_choice, features=features,
                agent=agent, on_delta=on_delta,
            )
            # The token counts come from the provider response; prompt and
            # output content are deliberately never passed to telemetry.
            try:
                observation.usage(extract_usage(result))
            except Exception:
                pass
            return result

    def _invoke_untraced(
        self, *, messages: list[dict], tools_schema: Any,
        tool_choice: str, features: Iterable[ModelInterceptor],
        agent: Any, on_delta: Callable[[str, str], None],
    ) -> Any:
        def invoke_provider() -> Any:
            if self.streaming and hasattr(self.provider, "stream_raw"):
                # Never replay a partially streamed model call as a nonstream
                # call: duplicated tokens or repeated tools are unsafe.
                return self.provider.stream_raw(
                    messages, tools=tools_schema, tool_choice=tool_choice,
                    on_delta=on_delta,
                )
            return self.provider.invoke_raw(
                messages, tools=tools_schema, tool_choice=tool_choice,
            )

        invoke_next = invoke_provider
        # Materialize one iterable once; reversed order preserves the prior
        # outer-to-inner feature pipeline.
        for feature in reversed(tuple(features)):
            prior = invoke_next
            invoke_next = lambda f=feature, p=prior: f.llm_intercept(
                agent, messages, tools_schema, tool_choice, p,
            )
        return invoke_next()
