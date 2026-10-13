# Web Trace timeline, cost estimates and operational health

## User-facing behavior

Web sidebar now includes **Trace**. For an active session with a local
TraceLogger, the page displays a **local Trace ID** (the TraceLogger's
session identifier, not necessarily the remote Langfuse/OTLP span ID),
recent Agent turns, nested steps and ordered model/tool operations.
It also shows known model/tool elapsed time, token totals, incomplete runs,
tool errors and empty-response retry counts.

`GET /api/sessions/{sid}/trace` provides the metadata in a bounded JSON
shape. The endpoint applies the existing local Web auth middleware and
the normal session ownership validation. It reads only the exact live
agent's trusted TraceLogger path, with parent-directory and filename checks;
clients cannot request arbitrary log paths. Persisted but inactive sessions
may show no available trace until a live Agent produces one.

**Security boundary:** local JSONL traces contain user messages, model
responses, file content and tool arguments. The server NEVER sends raw
trace events back to the browser. It only extracts metadata using a
fixed allowlist. It discards prompt/response text, tool parameter values,
file paths, exception stack traces, and raw tool-call IDs. The file
reader limits file size to the last 4 MiB and parses at most 5,000
events; partial data is flagged `truncated=true`. The projection is
designed for one local operator, not multi-tenant RBAC.

## Cost

Configure explicit per-model (or approximate profile) USD rates:

```dotenv
TRACE_INPUT_USD_PER_MILLION=1.50
TRACE_OUTPUT_USD_PER_MILLION=6.00
```

Both values must be present, finite and nonnegative. Otherwise the UI
shows only token counts and "未配置费率". The estimate formula is
`(input_tokens * input_rate + output_tokens * output_rate) / 1_000_000`.
It is NOT an invoice; caching discounts, reasoning-token accounting,
provider minimums or per-model rate changes may differ. The current
rates are global per server and should only be used when all traced
models share an appropriate price profile. Model-specific rate tables
and provider billing reconciliation remain future work.

## Quality / evaluation

Displayed "运行健康度" is a **diagnostic heuristic** only:
- `needs_review`: run failed/cancelled or tools returned errors
- `warning`: turn still running or an empty-response retry occurred
- `healthy`: completed without these known errors

This explicitly **does not score answer correctness, code quality or
alignment**. A useful future evaluation flow needs human review, tests,
explicit pass/fail criteria and/or a separately configured evaluator.

## Trace collection details

`CodeAgent._invoke_llm_with_retry` now records a small `model_call`
local event with elapsed time, status, model name and usage, including
the failure branch. Existing `model_output` events remain unchanged
for local audit compatibility. The Web analyzer prefers `model_call`
for usage when present and falls back to `model_output` for older
traces; token counts must never be double counted.

For advanced remote correlation see
`docs/design/2026-10-13-optional-otel-langfuse-tracing.md`.
The Web panel currently uses the **local Trace ID** and does not claim
real-time Langfuse linkability without corresponding remote trace IDs.

## Tests

`tests/test_trace_web_analysis.py` covers:
- a full local turn with tool and model events and secret values,
- model token accounting without double counting,
- optional cost estimate (including unconfigured case),
- missing, invalid or unrelated session trace files,
- oversized/malformed JSONL bounded parsing.

The build CI also compiles the React Trace panel.
