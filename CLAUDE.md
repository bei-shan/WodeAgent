# MyCodeAgent (WodeAgent) — Project Guide

## Project Overview

An AI code agent framework for learning and experimentation. Built around ReAct loops, tool protocols, context engineering, subagent delegation, team collaboration, TUI interaction, and observability.

- **Language**: Python 3.12+
- **LLM Providers**: OpenAI / DeepSeek / Qwen / Zhipu / Kimi / Modelscope / SiliconFlow / Ollama / vLLM
- **TUI**: prompt_toolkit + Rich
- **Storage**: JSONL + JSON files

## Project Structure

```
agents/          - Main agent implementations (CodeAgent with ReAct loop)
core/            - Core runtime, LLM client, context engine, team engine, worktree
  ├── context_engine/  - History management, compression, trace logging, truncation
  ├── team_engine/     - AgentTeams: manager, supervisor, worker, task board, protocol
  ├── worktree/        - Git worktree isolation
  └── skills/          - Skill loading mechanism
tools/           - Tool system: base class, registry, permission gate, circuit breaker
  ├── builtin/       - 31 built-in tools (file ops, search, bash, subagent, teams, etc.)
  └── mcp/           - MCP client, adapter, loader, protocol
tui/             - Rich streaming, @-mention completer, permission dialog, status line
prompts/         - System prompts, tool descriptions, output style templates
tests/           - 70+ test files, 240+ test cases (pytest)
scripts/         - CLI entry points and first-run wizard
skills/          - Skill definitions (SKILL.md per skill)
docs/            - Design documentation and protocol specs
memory/          - Trace/session/todo output (local)
tool-output/     - Long tool output persistence (7-day retention)
```

## Build, Test, and Development

```bash
# Install
pip install -r requirements.txt

# Run interactive CLI
python scripts/chat_test_agent.py

# With specific model
python scripts/chat_test_agent.py --model gpt-4o --provider openai --api-key sk-xxx

# Plan mode
python scripts/chat_test_agent.py --plan

# First-run setup wizard
python scripts/first_run_wizard.py

# Run tests
pytest tests/ -v

# Run with specific ignores (skip known flaky tests)
pytest tests/ --ignore=tests/test_agent_teams_parallel.py \
              --ignore=tests/test_team_worker.py \
              -k "not test_grep_success_no_matches and not test_restore_requeues_running_work_items"
```

## Coding Style

- **Indentation**: 4 spaces (PEP 8)
- **Naming**: Classes `PascalCase`, functions/variables `snake_case`, constants `UPPER_SNAKE_CASE`
- **Type hints**: Required for function parameters and returns
- **Docstrings**: Triple quotes for classes and functions

## Commit Convention

- `feat:` - New features
- `docs:` - Documentation updates
- `fix:` - Bug fixes
- Example: `feat: token budget tracking — parse, track, and display budget usage`

## Key Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `LLM_PROVIDER` | (required) | LLM provider name |
| `LLM_API_KEY` | (required) | API key |
| `LLM_MODEL_ID` | (required) | Model ID |
| `LLM_BASE_URL` | (required) | API base URL |
| `CONTEXT_WINDOW` | 200000 | Context window size |
| `COMPRESSION_THRESHOLD` | 0.8 | Trigger ratio for history compression |
| `MIN_RETAIN_ROUNDS` | 10 | Minimum rounds kept after compression |
| `SUBAGENT_MAX_STEPS` | 15 | Max ReAct steps for subagents |
| `TOOL_OUTPUT_MAX_LINES` | 2000 | Truncation threshold (lines) |
| `TRACE_ENABLED` | true | Enable trace logging |
| `VCR_ENABLED` | false | Enable LLM recording/replay |
| `ENABLE_AGENT_TEAMS` | false | Enable team collaboration |
| `PERMISSION_SOFT_SANDBOX` | true | Enable soft sandbox |

## MCP Configuration

Configure in `mcp_servers.json`:
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

## Skills Convention

```
skills/<skill-name>/SKILL.md
```

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
