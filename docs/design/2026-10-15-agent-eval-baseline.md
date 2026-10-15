# Ridge Code deterministic Agent evaluation baseline (2026-10-15)

## What this measures

`scripts/agent_eval.py` is an **offline scripted** benchmark that executes the
real `CodeAgent.run()` loop, ToolRegistry, file tools and context engine inside
disposable temporary project directories. It scores observable artifact state,
preserved files, turn completion, expected model-call count and exit status;
it does **not** call an external LLM/MCP provider or attempt answer-quality
grading using a simulated model.

| Fixture ID | Execution exercised | Pass criterion |
| --- | --- | --- |
| `cross_file_refactor` | Adjacent Read calls, followed by Edit across source and documentation | Both file bytes match pinned expected outputs |
| `tool_error_recovery` | Missing input tool failure, subsequent Write | Explicit recovery report written; original notes unchanged |
| `multiturn_continuation` | Write in first turn, Read in later turn | File persists and scripted multi-turn call count holds |
| `outside_workspace_denial` | Attempt to write `../private.txt` | Sentinel outside workspace untouched |
| `context_compaction_continuity` | History summary, serialize/restore, cursor | Original goal survives exactly once with cursor preservation |

## Repeatability and privacy

```bash
python scripts/agent_eval.py --json
python scripts/agent_eval.py --case cross_file_refactor --json
python -m pytest tests/test_agent_eval.py -q
```

The exit status is 0 only if *all* selected cases pass. Reports contain case
IDs, numeric turn/model-call counts and duration, categorical failures and
schema version. They deliberately exclude prompts, files, paths, tokens and
tool results. The evaluator has no model API keys, cloud credentials or web
dependencies. Runtime side effects are confined to new temporary directories.

The fixture definitions and exact expected file output are version-controlled
and deterministic. CI runs the harness in the Python full-regression suite
and curated API smoke tests. A green benchmark is **not** an accuracy score for
OpenAI, Anthropic or any other model.

## Next evaluation layers

1. Fault injection beyond scripted missing files: permission cancellation,
   MCP disconnection, idempotency boundaries, process/VM termination, snapshot
   replay and concurrent-agent ownership. Add named fixtures and independent
   scorers for each; never grade model prose by fragile exact-match.
2. Opt-in real-provider tasks: multiple models, fixed repository snapshots,
   explicit per-task token/cost ceilings, reproducible test commands and
   redacted provenance, latency and outcome reporting.
3. Compare context compaction, tool scheduling and subagent modes under the
   same task conditions; report error bars across multiple seeds, rather than
   claiming quality from one prompt.
4. Publish a documented non-regression threshold and redacted baseline
   artifacts tied to a SHA before promoting a v0.1 release candidate.

This is the foundation for [Issue #71](https://github.com/bei-shan/WodeAgent/issues/71),
not proof that the full real-provider evaluation task is complete.
