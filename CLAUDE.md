# MyCodeAgent (WodeAgent) — Project Guide

## Project Overview

An AI code agent framework for learning and experimentation. Built around ReAct loops, a pluggable feature system, tool protocols, context engineering, subagent delegation, AgentTeams collaboration, a thread-decoupled session runtime, and dual TUI / Web frontends. Storage is plain JSON + JSONL; LLM access is OpenAI-compatible with multi-provider routing.

- **Language**: Python 3.12+
- **LLM Providers**: OpenAI / DeepSeek / Qwen (DashScope) / Zhipu (GLM) / Kimi (Moonshot) / ModelScope / SiliconFlow / Ollama / vLLM
- **TUI**: prompt_toolkit + Rich
- **Web/Desktop**: FastAPI service + Vite/React/TS/Tailwind frontend
- **Storage**: JSON snapshots + JSONL session tree

## Project Structure

```
agents/                  - CodeAgent (ReAct loop, integration points)
core/                    - Core engine
  ├── llm.py               - HelloAgentsLLM (multi-provider, streaming)
  ├── config.py            - Pydantic Config (50+ fields)
  ├── events.py            - AgentEvent / EventType / EventSink
  ├── session_manager.py   - Disk session catalog (memory/sessions/)
  ├── tool_bootstrap.py    - Tool discovery + DI
  ├── plugin_loader.py     - .mycode/plugins/ discovery
  ├── model_profiles.py    - Model profiles + pointers
  ├── budget_tracker.py    - Token budget
  ├── background_task.py   - Daemon-thread sub-agent runner
  ├── hook_system.py       - Lifecycle hooks
  ├── output_styles.py     - Output style manager
  ├── vcr.py               - LLM record/replay
  ├── response_parser.py / message.py / env.py / env_helpers.py / constants.py / exceptions.py / agent.py
  ├── context_engine/      - history_manager, context_builder (Late Binding),
  │                         observation_truncator, summary_compressor,
  │                         trace_logger, trace_sanitizer, input_preprocessor, jsonl_store
  ├── features/            - 11 AgentFeature plugins (worktree, mcp, agent_teams,
  │                         delegate, budget, plan_mode, background_task,
  │                         output_style, hooks, vcr, session) + base.py
  ├── runtime/             - SessionController, AgentSession (per-session thread + event queue)
  ├── team_engine/         - manager, supervisor, worker, store, task_board, message_router,
  │                         approval, execution, turn_executor, tmux_orchestrator, events
  ├── worktree/            - Git worktree manager
  └── skills/              - SkillLoader (two-layer: source + runtime)
tools/                   - Tool system
  ├── base.py              - Tool / ToolParameter / ErrorCode
  ├── registry.py          - ToolRegistry (OpenAI schema, optimistic lock)
  ├── permission_gate.py   - Soft sandbox + broker
  ├── circuit_breaker.py   - Per-tool CB
  ├── builtin/             - 33 built-in tools
  └── mcp/                 - config, client, adapter, loader, protocol
tui/                     - streaming, mention_completer, permission_dialog, status_line
prompts/                 - agents_prompts/, tools_prompts/ (33 files), output_styles/
scripts/                 - chat_test_agent.py, first_run_wizard.py, slash_commands.py
desktop/                 - service/ (FastAPI app + schemas), web/ (Vite + React + TS + Tailwind)
utils/                   - helpers, logging, serialization, ui_components (EnhancedUI)
tests/                   - 79 test_*.py (~936 cases), conftest.py, fixtures/, utils/
docs/                    - PROJECT_OVERVIEW, IMPLEMENTATION_SUMMARY, design/, agent_teams/, plans/, archive/
skills/                  - Built-in skill sources (SKILL.md per skill)
memory/                  - traces/, sessions/ (runtime output)
tool-output/             - Truncated tool output persistence (7-day retention)
.mycode/                 - hooks.json, plugins/ (user-managed)
.mycodeagent/            - skills/, sessions/<sid>/ (runtime workspace)
.teams/ .tasks/ .worktrees/ - AgentTeams / task board / worktree stores
mcp_servers.json         - MCP server config
.env / .env.example      - Environment configuration
```

## Build, Test, and Development

```bash
# Install
pip install -r requirements.txt

# First-run interactive wizard (auto-triggers if .env missing)
python scripts/first_run_wizard.py

# Run TUI
python scripts/chat_test_agent.py
python scripts/chat_test_agent.py --model gpt-4o --provider openai --api-key sk-xxx
python scripts/chat_test_agent.py --plan          # plan mode
python scripts/chat_test_agent.py -c              # continue last session
python scripts/chat_test_agent.py -r <id|index>   # resume specific session

# Run desktop web service (FastAPI)
uvicorn desktop.service.app:create_app --factory --reload

# Run desktop frontend (Vite)
cd desktop/web && npm install && npm run dev

# Tests
pytest tests/ -v
pytest tests/ --ignore=tests/test_agent_teams_parallel.py \
              --ignore=tests/test_team_worker.py \
              -k "not test_grep_success_no_matches and not test_restore_requeues_running_work_items"
```

## Coding Style

- **Indentation**: 4 spaces (PEP 8)
- **Naming**: Classes `PascalCase`, functions/variables `snake_case`, constants `UPPER_SNAKE_CASE`
- **Type hints**: Required for function parameters and return values
- **Docstrings**: Triple quotes for classes and public functions

## Commit Convention

- `feat:` - New features
- `fix:` - Bug fixes
- `docs:` - Documentation updates
- Example: `feat: token budget tracking — parse, track, and display budget usage`

## Key Environment Variables

Authoritative source: `.env.example` and `core/config.py`. Selected highlights:

| Variable | Default | Description |
|----------|---------|-------------|
| `LLM_PROVIDER` | openai | Provider key (openai/deepseek/qwen/zhipu/kimi/modelscope/siliconflow/ollama/vllm) |
| `LLM_API_KEY` | (required) | API key (per-provider keys like `DEEPSEEK_API_KEY` also honored) |
| `LLM_MODEL_ID` | gpt-3.5-turbo | Model ID |
| `LLM_BASE_URL` | (provider default) | Override base URL |
| `LLM_STREAMING` | true | Enable Rich Live token streaming |
| `MAX_STEPS` | 50 | Max ReAct steps for main agent |
| `SUBAGENT_MAX_STEPS` | 15 | Max ReAct steps for subagents |
| `CONTEXT_WINDOW` | 128000 | Context window size |
| `COMPRESSION_THRESHOLD` | 0.8 | Trigger ratio for history compression |
| `MIN_RETAIN_ROUNDS` | 10 | Minimum rounds kept after compression |
| `SUMMARY_TIMEOUT` | 120 | LLM summary timeout (s) |
| `TOOL_OUTPUT_MAX_LINES` | 2000 | Tool-output line cap |
| `TOOL_OUTPUT_MAX_BYTES` | 51200 | Tool-output byte cap |
| `TOOL_OUTPUT_TRUNCATE_DIRECTION` | head | head / tail / head_tail |
| `TRACE_ENABLED` | true | Write JSONL + HTML traces |
| `TRACE_DIR` | memory/traces | Trace output dir |
| `TRACE_SANITIZE` | true | Scrub secrets/paths from traces |
| `VCR_ENABLED` | false | Record/replay LLM calls |
| `ENABLE_AGENT_TEAMS` | false | Master switch for AgentTeams |
| `TEAMMATE_MODE` | auto | auto / in-process / tmux |
| `TEAM_LLM_MAX_CONCURRENCY` | 4 | Global LLM semaphore for team workers |
| `TEAM_WORKER_MAX_STEPS` | 8 | Per-turn worker step cap |
| `TEAM_LLM_MAX_RETRIES` | 2 | Retryable-exception retries |
| `TEAM_LLM_RETRY_BACKOFF` | 0.2 | Base backoff seconds (+10% jitter) |
| `TEAM_HEARTBEAT_TIMEOUT` | 300 | Stale work-item reaper threshold |
| `MCP_CONNECT_MODE` | manual | startup / manual / disabled |
| `WORKTREE_STORE_DIR` | .worktrees | Git worktree root |
| `WORKTREE_BASE_REF` | fresh | Base ref for new worktrees |
| `AGENT_OUTPUT_STYLE` | default | default / explanatory / learning |
| `CIRCUIT_FAILURE_THRESHOLD` | 3 | Per-tool failure threshold |
| `CIRCUIT_RECOVERY_TIMEOUT` | 300 | Half-open recovery (s) |
| `SKILLS_PROMPT_CHAR_BUDGET` | 12000 | Skill index char budget |
| `AGENT_INTERACTIVE` | true | Allow interactive permission prompts |

Per-provider keys (`OPENAI_API_KEY`, `DEEPSEEK_API_KEY`, `DASHSCOPE_API_KEY`, `MODELSCOPE_API_KEY`, `KIMI_API_KEY` / `MOONSHOT_API_KEY`, `ZHIPU_API_KEY` / `GLM_API_KEY`, `SILICONFLOW_API_KEY`, `OLLAMA_HOST`, `VLLM_HOST`) and model-profile env (`MODEL_PROFILES`, `MODEL_<NAME>_ID/PROVIDER/API_KEY/BASE_URL`, `MODEL_POINTER_MAIN/TASK/COMPACT`) are documented in `.env.example`.

## MCP Configuration

Configure in `mcp_servers.json` (also accepted: `.mcp.json`, `mcp.json`, or `MCP_SERVERS` env var):

```json
{
  "mcpServers": {
    "tool-name": {
      "command": "npx",
      "args": ["-y", "some-mcp-server"]
    }
  }
}
```

Connect modes (`MCP_CONNECT_MODE`): `startup` (sync at boot), `manual` (background threads, retry on first call), `disabled`.

## Skills Convention

Two-layer design:
- **Source** (`skills/<name>/SKILL.md`) — git-tracked, shipped with the repo.
- **Runtime** (`.mycodeagent/skills/<name>/SKILL.md`) — writable, auto-seeded from source on first scan, runtime entries override source on name collision.

SKILL.md format:
```markdown
---
name: skill-name
description: Skill description
---
# Skill Title
Instructions here...
$ARGUMENTS
```

Name pattern: `^[a-z0-9]+(?:-[a-z0-9]+)*$`. Loaded via `core/skills/skill_loader.py`; invoked through the `Skill` tool.

## Plugins Convention

Drop plugins under `.mycode/plugins/<plugin-name>/` with a `plugin.json` manifest:

```json
{
  "name": "my-plugin",
  "version": "0.1.0",
  "description": "...",
  "features": {
    "hooks": { "PreToolUse": [ ... ] },
    "skills": ["./skills/foo"],
    "output_styles": ["./styles/foo.md"],
    "custom_features": ["./my_feature.py"]
  }
}
```

`core/plugin_loader.py` discovers sorted subdirs (skipping `.`/`_` prefixes), merges hooks into `.mycode/hooks.json`, refreshes the skill index, reloads output styles, and loads custom Python features (first `AgentFeature` subclass per file) via `importlib`. Each feature surface is wrapped as an `AgentFeature` so it slots into the same lifecycle as the built-ins.

## Hooks Convention

Lifecycle hooks live in `.mycode/hooks.json` and are managed by `HookFeature` (`core/features/hooks.py`). Supported phases: `SessionStart`, `PreToolUse`, `PostToolUse`, `SessionEnd`. Hooks can block tool execution, inject system messages, or attach one-shot session context. Editable via the web UI (`GET/PUT /api/hooks`).

## Web / Desktop App

`desktop/service/app.py` exposes a FastAPI service (~40 REST routes + 1 WebSocket):

- **Sessions**: create/list/get/activate/delete/rename/pin/history
- **Turn control**: `POST /messages`, `POST /interrupt`, `POST /upload`
- **Permission / AskUser**: `POST /permissions/{rid}/resolve`, `POST /ask-user/{rid}/answer`
- **Config / teams / files / models / tools / mcp / skills / hooks**: full CRUD
- **WebSocket**: `ws://.../api/sessions/{sid}/stream` — server-to-client JSON frames `{type, payload, step}`. Forwarded events: the 8 core lifecycle events (`run.started`, `run.finished`, `step.started`, `llm.started`, `llm.completed`, `tool.started`, `tool.completed`, `assistant.final`) plus 4 session-layer events (`permission.requested`, `ask_user.requested`, `turn.completed`, `error`). Payloads are sanitized (underscore-prefixed keys stripped, strings >50KB truncated). CORS is permissive (`*`); no auth.

Frontend: `desktop/web/` (Vite + React + TS + Tailwind). Run `npm run dev` for hot reload.

## Runtime + Event System

The agent core is decoupled from any UI via `core/runtime/session_controller.py`. `SessionController` manages a dict of `AgentSession` instances; each session owns a daemon worker thread, a `queue.Queue[AgentEvent]`, and a `_busy` guard. `CodeAgent.event_sink` pushes `AgentEvent`s into the queue; permission and AskUser prompts are routed through `_SessionPermissionBroker` (120s timeout) and `_SessionAskUserFunc` (300s timeout) which block on `threading.Event` until the UI calls `session.resolve_permission(rid, decision)` / `session.answer_ask_user(rid, answer)`. The TUI bypasses `SessionController` (synchronous on the main thread) and wires `PermissionDialog.ask` / `prompt_toolkit` directly into `PermissionGate._broker` and `AskUser._input_func`; the web service consumes the same broker contract via HTTP + WebSocket. Both UIs share one event vocabulary, proving the runtime is UI-agnostic.

Feature composition is driven by `core/features/`: 11 ordered `AgentFeature` plugins (worktree → mcp → agent_teams → delegate → budget → plan_mode → background_task → output_style → hooks → vcr → session) hook into `init` / `post_init` / `runtime_blocks` / `pre_tool_use` / `post_tool_use` / `llm_intercept` / `cleanup`, replacing what used to be hand-wired logic in `CodeAgent`. New capabilities should land as features, not as edits to `agents/codeAgent.py`.