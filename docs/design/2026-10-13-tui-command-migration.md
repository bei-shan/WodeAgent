# TUI v2 command migration — verified state (2026-10-14)

The retired Python Rich/prompt_toolkit UI previously implemented slash
commands inside its own Agent process. TypeScript/OpenTUI uses canonical
FastAPI/WS application services and must not mutate Python engine objects.

## Implemented through shared server APIs

- Session commands: `/new`, `/sessions`, `/use N`, `/rename TITLE`,
  `/pin`, `/delete confirm`
- Runtime controls: `/models`, `/model PROFILE`, `/plan on|off`,
  `/teams on|off`, `/stop`, `/help`
- Branches: `/tree`, `/fork NODE_ID`. These use persistent session-tree
  APIs and retain original branches. Forking requires an idle turn.
- Portable conversation archives: `/export FILE`, `/import FILE` use
  versioned JSON APIs. The local TUI writes/reads its own machine's files,
  with size limits and refusal to overwrite an existing archive. These
  archives preserve user-visible conversation and tree labels **only**:
  they do not restore credentials, tools, server settings, worktree, cache,
  full system prompts or Teams runtime state. See
  [session portability](2026-10-13-session-portability.md).

## Remaining / not promised

- Legacy `/style` and `/budget` commands are not supported by the new
  terminal client. Implement explicit versioned server APIs and tests
  before reintroducing them; do not silently send them to the LLM.
- Legacy private snapshot commands are not implemented. The portable
  conversation archive described above is **not** a lossless Agent backup.
- OpenTUI remains experimental and depends on an accessible Python backend;
  terminal end-to-end test coverage does not yet imply all platform/TTY
  environments have been tested.

Pending Permission and AskUser prompts take priority over slash commands;
the reply must be delivered to the waiting Agent. Unknown slash commands
remain local errors and are not forwarded as model messages.

## Verification

- `tui-v2/src/commands.ts` is the current command parser.
- `tui-v2/test/portable.test.ts` checks portable archive command behavior.
- `tui-v2/test/pty_smoke.py` includes real PTY + HTTP roundtrip checks.
- `.github/workflows/tui-v2.yml` runs typecheck, unit and PTY checks.

Keep this list aligned with the parser, server API and CI before release.
