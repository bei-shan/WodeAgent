exit_plan_mode_prompt = """## ExitPlanMode

Exit plan-only mode and restore full tool access.

Parameters:
- plan: (required) The plan you produced while in plan mode.  This is
  injected into the system prompt so you can follow it during execution.

Behaviour:
- All tools (Write, Edit, Bash, Task, etc.) are re-enabled.
- The *plan* text is added to your working context.
- You should now execute the plan step by step.

Use this when:
- You have finished analysing and have a clear, actionable plan.
- The user has approved your plan (if you presented it to them).
"""
