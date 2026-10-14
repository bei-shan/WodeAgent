# Agent resource stability / soak acceptance — v0.1

## GitHub Actions manual acceptance evidence

The workflow [Agent resource soak (manual)](../../.github/workflows/agent-resource-soak.yml)
can be launched from GitHub **Actions → Agent resource soak (manual) → Run
workflow** on a selected commit or branch. Choose 30 or 60 minutes. It
uses a read-only repository token, an isolated Linux runner and the
`FakeAgent` provider; no model credentials or remote MCP endpoints are
required. The workflow stores `agent-soak-report.json` and
`soak-environment.txt` as an artifact retained for 30 days. Preserve the
workflow run URL, tested Git SHA and artifact together when accepting a
release candidate.

Duration-mode work is paced to at most one cycle per second by default
to avoid filling ephemeral runner disk with unbounded session histories.
Inputs reject nonfinite durations and runs over two hours. A manual
workflow definition alone is **not** evidence that 30-minute execution
has passed; sign off only when the actual run finished successfully.

## Offline repeatability

Use a dedicated workstation or CI runner with Python dependencies installed:

```bash
python scripts/agent_soak.py --iterations 20 --json
python scripts/agent_soak.py --duration-seconds 1800 --json > soak-report.json
```

The benchmark runs real `SessionController` worker threads, session creation
and cleanup, real EventJournal replay and durable turn outcome publication
in a disposable temporary working directory. It uses **FakeAgent**, never
calls a live LLM provider, never touches the operator's project state and
never sends network traffic.

It reports cycle count, elapsed time, baseline/end/peak resource metrics
where available, growth in process threads, open file descriptors and RSS
and a bounded list of failure labels. RSS and FD metrics are available on
Linux via `/proc`; null elsewhere. The default strict gates are:

- Thread count growth: at most 5
- Open file descriptor growth: at most 12 (where supported)
- RSS growth: at most 96 MiB (where supported)
- All sessions close safely, no workers left busy
- Turn terminal outcomes and two independent replay subscriptions agree

The **duration mode** loops for at least the configured duration. A
separate `--iterations` mode is suited to deterministic short CI checks
and does not count as a soak acceptance.

### Evidence record

Keep `soak-report.json` with host OS, CPU, RAM, Python version,
Git SHA, model/provider configuration and any sanitized MCP/OTel health
observations. Repeat on Linux, macOS and Windows where supported. Do not
upload API keys, prompts, OAuth tokens or raw tool input/results.

## Real-provider and MCP long-run release acceptance (still pending)

Offline mock testing cannot prove actual provider resource stability.
The release operator must run a **separate** 30+ minute authenticated Agent
session with a known safe read-only workload and a real MCP server. Check
reconnect, permission cancellation, callback/token refresh, tool timeouts,
session stop/restart and trace exporter backpressure. Capture sanitized
CPU/RSS/FD/thread telemetry and check for growth over the steady-state
window. Use a controlled disposable workspace and terminate the run on
unexpected mutations. This phase supplies reproducible lower-bound
contracts; it does not falsely mark real-provider acceptance complete.

If results exceed the offline thresholds, do not tag v0.1.0.
