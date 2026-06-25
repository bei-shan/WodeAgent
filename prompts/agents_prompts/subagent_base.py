"""Shared base rules for all subagent types — deduplicated from 4 separate files."""

SUBAGENT_BASE_RULES = """Rules
- STRICTLY read-only. Do NOT create, edit, or delete files.
- Do NOT use Bash.
- Do NOT call Task or attempt to spawn other agents.
- Use only the tools provided (LS, Glob, Grep, Read).
- Return file paths relative to the project root.
- Use OpenAI function calling for tools. Do NOT output Action/ToolName text or `<tool_call>` tags."""
