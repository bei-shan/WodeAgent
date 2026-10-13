# ModelGateway typed provider boundary

core/runtime/model_gateway.py now owns the choice between invoke_raw and
stream_raw and the ordered Feature.llm_intercept chain. CodeAgent retains its
empty-response retry, token budgets, traces, reasoning display, event
emission and persisted outcomes. This separates LLM provider invocation from
the AgentLoop/ContextRuntime/ToolRuntime components.

Rules:
- Use stream_raw only when enabled and supported by the provider.
- Never silently fall back to invoke_raw after stream_raw raises; a partial
  response would be replayed, potentially duplicating tools or tokens.
- Preserve outermost-to-innermost interceptor ordering.
- ModelGateway never imports CodeAgent or presentation modules.
- Existing Web, TUI and stream event envelopes remain unchanged.

Next adapters: explicitly scoped ContextStore and Config, structured
turn command/results to reduce use of the transitional AgentLoopPort
host callbacks, and end-to-end real terminal validation.
