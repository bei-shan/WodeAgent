"""SendMessage tool prompt."""

send_message_prompt = """
Tool name: SendMessage
Send a message to a teammate inbox inside a team.

Usage
- Use to communicate with teammates in AgentTeams mode.
- Messages are persisted to the teammate's inbox and acknowledged.
- Supports broadcast, shutdown, and plan approval protocols.

Parameters (JSON object)
- team_name (string, required)
  Team name.
- from_member (string, required)
  Sender member name.
- to_member (string, required)
  Receiver member name (ignored by broadcast routing).
- text (string, required)
  Message text.
- type (string, optional, default "message")
  message | broadcast | shutdown_request | shutdown_response | plan_approval_response
- summary (string, optional)
  Short summary. Required when type=message|broadcast.
- request_id (string, optional)
  Correlation id. Required for shutdown_response and plan_approval_response.
- approved (boolean, optional)
  Approval decision for plan_approval_response.
- feedback (string, optional)
  Optional feedback for plan_approval_response.

Response Structure
- status: "success" | "error"
  - "success": message sent and persisted to inbox
  - "error": invalid parameters, team not found, or internal error
- data (success):
  - message_id: str — the created message id
  - status: str — "pending" | "delivered"
  - type: str — the message type used
  - recipient_count: int — number of recipients (broadcast)
  - request_id: str — correlation id if provided
  - summary: str — message summary if provided
- error (error):
  - code: INVALID_PARAM | NOT_FOUND | INTERNAL_ERROR

ACK status lifecycle
- pending: message created
- delivered: persisted to inbox
- processed: teammate acknowledged processing

Error Codes
- INVALID_PARAM: missing required field or invalid type
- NOT_FOUND: team or member not found
- INTERNAL_ERROR: unexpected failure

Examples
1. Send a direct message:
   {"team_name": "my-team", "from_member": "lead", "to_member": "dev1",
    "text": "Please review PR #42", "summary": "PR review request", "type": "message"}

2. Broadcast to all members:
   {"team_name": "my-team", "from_member": "lead", "to_member": "*",
    "text": "Sprint planning in 10 min", "summary": "Sprint planning reminder", "type": "broadcast"}

3. Respond to a plan approval request:
   {"team_name": "my-team", "from_member": "lead", "to_member": "architect",
    "text": "Approved", "type": "plan_approval_response", "request_id": "req-001", "approved": true,
    "feedback": "Looks good, proceed"}
"""
