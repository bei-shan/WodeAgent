task_output_prompt = """## TaskOutput

Retrieve the result of a background task launched with
Task(run_in_background=true).

Parameters:
- task_id: (required) The task ID returned when the background task was started.

Returns:
- status: "completed" | "failed" | "running" | "not_found"
- result: The sub-agent's final output (if completed)
- tool_usage: Tool call counts (if completed)
- error: Error message (if failed)

Use this to poll background tasks.  The runtime block shows which
tasks are ready (✓ completed) or still running (⏳).
"""
