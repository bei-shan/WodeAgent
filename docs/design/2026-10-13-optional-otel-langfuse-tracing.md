# Optional Langfuse v4 / OpenTelemetry tracing — 2026-10-13

## Goal and ownership

Ridge Code remains local-first. The existing JSONL+HTML TraceLogger owns
full local execution audit and is unchanged. A second, opt-in **metadata-only**
remote telemetry adapter captures timing, parent/child relationships, tool
names, success/failure and aggregate LLM token usage. It does not replace or
depend on local TraceLogger being enabled.

`core/runtime/telemetry.py` selects one of three modes from
`AGENT_TELEMETRY_BACKEND`:

- `off` (default): no external SDK imports or network/export side effects
- `langfuse`: native Langfuse Python SDK v4 backed by OpenTelemetry; Agent
  turns appear as agents, model calls as generations with model/token usage,
  and tool calls as tool observations
- `otlp`: vanilla OpenTelemetry tracer with OTLP-over-HTTP batch export to
  a preconfigured collector; does not replace the global tracer provider

Each turn has an `agent.turn` root span. `ModelGateway.invoke` records a
nested `agent.model` generation for each actual provider call (including
retries), `ToolRuntime.execute` records `agent.tool` spans. OTel context
propagation supplies correct nesting for callers that run in the same
context. Thread worker context propagation already exists in
`core/runtime/tool_scheduler.py`.

## Installation and configuration

```bash
python -m pip install -r requirements.txt
python -m pip install -r requirements-observability.txt
```

**Langfuse Cloud or self-hosted (SDK v4)**

```dotenv
AGENT_TELEMETRY_BACKEND=langfuse
LANGFUSE_PUBLIC_KEY=pk-lf-...
LANGFUSE_SECRET_KEY=sk-lf-...
LANGFUSE_BASE_URL=https://cloud.langfuse.com
```

The SDK's background export is allowed to batch; no synchronous network
flush occurs on each Agent turn. A flush is registered for process exit.
This allows long-running web/TUI sessions to avoid remote exporter latency.

**OpenTelemetry Collector / Langfuse-compatible OTLP ingestion**

```dotenv
AGENT_TELEMETRY_BACKEND=otlp
OTEL_EXPORTER_OTLP_TRACES_ENDPOINT=https://collector.example.org/v1/traces
# Optional, depending on the collector:
OTEL_EXPORTER_OTLP_HEADERS=authorization=Bearer%20redacted
```

OTLP is HTTP/protobuf with a bounded batch queue. The explicit
`OTEL_EXPORTER_OTLP_TRACES_ENDPOINT` must be HTTPS or local-loopback HTTP.
Use `langfuse` mode for Langfuse-specific generations/usage and semantic
enrichment; OTLP mode is intended for backend-neutral collectors.

**Turn it off**

```dotenv
AGENT_TELEMETRY_BACKEND=off
```

Removing/omitting the setting is identical to `off`. A local trace can be
enabled independently using `TRACE_ENABLED=true`.

## Privacy: strict allowlist, not regex-only redaction

**Never exported by the adapter**: user input, system/developer prompts,
model output, tool arguments/results, source content, filesystem paths,
API keys, session IDs, raw exception messages or stack traces.

**Allowed**:
`ridge.tool.name`, `ridge.tool.status`, `ridge.turn.status`,
`ridge.model`, numeric `gen_ai.usage.*` counters and span timing/name.
Attribute names are restricted to a local allowlist. Attribute values are
strict primitive types, and strings are normalized and length-limited.
No recording of raw JSONL TraceLogger payloads takes place.
The exporter exception path passes `(None, None, None)` to span exit so
automatic OTel error recording cannot leak exception messages or stacks.
Export failures log only a generic warning and do not change Agent outcome.

Local JSONL tracing may contain more information after its own sanitizer.
Do not mistake the local trace redaction policy for remote export policy.
For especially sensitive projects, disable local tracing too.

## Verification

`tests/test_optional_telemetry.py` covers default-off mode, real
OpenTelemetry in-memory parentage and token attributes, native Langfuse
v4 API shape with a mock client, explicit content exclusion, zero message
export even during exceptions, and fallback when the exporter fails.

`telemetry-sdk-smoke` CI installs optional dependencies and exercises
instrumentation with no live Langfuse or collector credentials. API smoke
and full Python regressions keep running even without those optional SDKs.

## Known limitations

- An external Langfuse/Collector server has **not** been exercised with real
  credentials in CI; it requires explicit operator configuration and a
  post-configuration end-to-end smoke check.
- Native SDKs can emit their own infrastructure metadata and SDK resource
  attributes in addition to those explicitly set by Ridge Code. The
  adapter never forwards content; inspect the exporter/security perimeter
  before hosting sensitive multi-tenant deployments.
- No runtime SDK-based instrumentation is enabled for independent non-turn
  operations or third-party plugin-level spans unless they run within an
  instrumented turn. Subagents generate their own turn roots unless an
  inherited context spans their invocation.
- No raw tool output recording, data payload sampling, live dashboards or
  remote evaluation scores are enabled here.
- Multiple telemetry backends are deliberately not simultaneously active.
  The `AGENT_TELEMETRY_BACKEND` setting is process-scoped and should be
  configured before starting Web or TUI server processes.

## Reference material

- https://langfuse.com/docs/observability/sdk/overview
- https://langfuse.com/docs/observability/sdk/instrumentation
- https://langfuse.com/docs/observability/features/token-and-cost-tracking
- https://opentelemetry.io/docs/languages/python/exporters/
