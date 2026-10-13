# Application Services Phase 3: Settings and Teams

Date: 2026-10-13

This increment extracts the configuration and Teams HTTP use cases into independent application services. The route methods now consume service methods rather than inspecting CodeAgent internals directly.

## SessionSettingsService

- Resolve AgentSession via SessionController and initialize its CodeAgent through a synchronized public ensure_agent() method.
- Keep the existing model, plan-mode and team flag HTTP contract and the 409 busy-turn gate.
- For enabling Agent Teams after initialization, instantiate TeamManager with the exact project root, LLM, registry and runtime mode used in AgentTeamsFeature.init.
- Preserve the configured ToolBootstrap on CodeAgent; refresh the team_manager/event_sink providers, then register team tools.

## TeamsService

- Keep listing and creation use cases out of FastAPI.
- Map outputs to transport-independent dicts; FastAPI validates response models.
- Return safe HTTP 500 envelopes on unexpected team creation failures rather than leaking raw exception messages.

## Test gates and deferred work

- EnsureAgent concurrent initialization test.
- GET/PUT settings, busy turn and team disabled API tests.
- Fake TeamManager/bootstrap test that verifies production constructor dependencies and tool registration path.
- Continue existing Web, auth, sandbox and browser smoke tests.
- Remaining work: workspace file APIs, process-global config state isolation, full AgentLoop/ToolRuntime interfaces and durable event storage.
