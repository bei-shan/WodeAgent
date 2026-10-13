# AgentLoop / ContextRuntime extraction

The Python engine now has three internal runtime components:

- core/runtime/agent_loop.py: step ordering, interruption, tool branch/final
  branch and max-step completion semantics through AgentLoopPort.
- core/runtime/context_runtime.py: compression trigger, compact trace events
  and per-step prompt building through ContextRuntimePort.
- core/runtime/tool_runtime.py: feature-aware tool dispatch through ToolFeature
  and ToolDispatcher protocols.

CodeAgent remains the owning session host: it assembles the features,
context, LLM provider, tools and UI event sink. Each extracted subsystem
depends on explicit ports instead of importing CodeAgent or presentation code.
CodeAgent keeps compatibility adapters, so existing subclasses, tests and
TUI/Web clients continue to call the same methods.

The new fake-port tests cover terminal and tool step ordering, cancellation,
max-step behavior, context compression trace details and import direction.
Existing model-recovery, sandbox, event lifecycle and Web/browser tests still
run unchanged.

These adapters are transitional. The next layer of dependency inversion is to
provide explicit ModelGateway and ContextStore instances instead of calling
private host methods through Protocols. This first migration deliberately
avoids changing model retries or persistence at the same time as loop flow.
