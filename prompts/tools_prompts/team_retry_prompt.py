team_retry_prompt = """## TeamRetry

Retry a failed work item by re-queuing it for the assigned teammate.

Parameters:
- team_name: (required) Name of the team
- work_id: (required) ID of the failed work item to retry

Returns:
- The work item with status changed back to "queued".
"""
