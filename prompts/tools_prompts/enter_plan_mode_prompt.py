enter_plan_mode_prompt = """## EnterPlanMode

Switch the agent into plan-only mode.  In this mode only **read-only**
tools are available (Read, Grep, Glob, LS, TodoWrite).  The agent
cannot create, edit, or delete files, run shell commands, or spawn
sub-agents.

Use this when:
- You need to analyse the codebase before making changes.
- You want to produce a structured plan for the user to review.
- The task is complex and benefits from upfront design.

While in plan mode, produce a clear, actionable plan.  When ready,
call ExitPlanMode to return to full tool access and begin execution.

Parameters: (none)
"""
