"""Team worker dispatch service: inbox, message-to-work and task auto-claim.

WorkerSupervisor owns live worker threads. TeamStore owns persisted inbox
and work items. ApprovalService owns approval records. This service only
orchestrates handing queued messages and board tasks to those owners.
"""
from __future__ import annotations

from typing import Any, Dict, Protocol

from .protocol import (
    EVENT_PLAN_APPROVAL_REQUESTED,
    EVENT_WORK_ITEM_ASSIGNED,
    MESSAGE_STATUS_PENDING,
    MESSAGE_STATUS_DELIVERED,
    MESSAGE_TYPE_MESSAGE,
    MESSAGE_TYPE_BROADCAST,
    MESSAGE_TYPE_SHUTDOWN_REQUEST,
    MESSAGE_TYPE_PLAN_APPROVAL_RESPONSE,
    sanitize_name,
)


class WorkerDispatchHost(Protocol):
    store: Any
    worker_supervisor: Any
    approval_service: Any
    _processed_by_member: dict
    _inbox_cursor: dict
    def _emit(self, team_name: str, event_type: str, payload: Dict[str, Any]) -> None: ...
    def _persist_approval(self, request: Dict[str, Any]) -> None: ...
    def _process_next_work_item(self, team_name: str, teammate_name: str) -> bool: ...
    def _dispatch_approved_plan_work(self, team_name: str, teammate_name: str) -> bool: ...
    def _enqueue_message_work(self, team_name: str, teammate_name: str, message_row: Dict[str, Any]) -> bool: ...
    def _claim_board_task_to_work_item(self, team_name: str, teammate_name: str) -> bool: ...
    def _send_shutdown_response(self, team_name: str, teammate_name: str, message_row: Dict[str, Any]) -> None: ...
    def _request_worker_stop(self, team_name: str, teammate_name: str) -> None: ...
    def _apply_plan_approval_response(self, team_name: str, teammate_name: str, message_row: Dict[str, Any]) -> None: ...
    def mark_message_processed(self, team_name: str, message_id: str, processed_by: str) -> dict: ...
    def claim_next_board_task(self, team_name: str, owner: str) -> dict | None: ...
    def _teammate_requires_plan_approval(self, team_name: str, teammate_name: str) -> bool: ...


class WorkerDispatchService:
    def __init__(self, owner: WorkerDispatchHost):
        self.owner = owner

    def _process_member_inbox(self, team_name: str, teammate_name: str) -> bool:
        owner = self.owner
        processed_ids = owner._processed_by_member[(team_name, teammate_name)]
        cursor = owner._inbox_cursor.get((team_name, teammate_name), 0)
        rows = owner.store.read_inbox_messages_from(team_name, teammate_name, start_line=cursor)
        did_work = False
        rows_processed = 0
        worker = owner.worker_supervisor.workers.get((sanitize_name(team_name), sanitize_name(teammate_name)))
        for row in rows:
            rows_processed += 1
            message_id = str(row.get("message_id") or "")
            if not message_id or message_id in processed_ids:
                continue
            status = str(row.get("status") or "")
            if status not in {MESSAGE_STATUS_PENDING, MESSAGE_STATUS_DELIVERED}:
                continue
            message_type = str(row.get("type") or MESSAGE_TYPE_MESSAGE)
            if message_type in {MESSAGE_TYPE_MESSAGE, MESSAGE_TYPE_BROADCAST}:
                did_work = owner._enqueue_message_work(team_name, teammate_name, row) or did_work
            elif message_type == MESSAGE_TYPE_SHUTDOWN_REQUEST:
                owner._send_shutdown_response(team_name, teammate_name, row)
                owner._request_worker_stop(team_name, teammate_name)
            elif message_type == MESSAGE_TYPE_PLAN_APPROVAL_RESPONSE:
                owner._apply_plan_approval_response(team_name, teammate_name, row)
            owner.mark_message_processed(team_name, message_id, processed_by=teammate_name)
            processed_ids.add(message_id)
            if worker is not None:
                worker.messages_processed += 1
            did_work = True
        did_work = owner._process_next_work_item(team_name, teammate_name) or did_work
        did_work = owner._dispatch_approved_plan_work(team_name, teammate_name) or did_work
        if not did_work:
            did_work = owner._claim_board_task_to_work_item(team_name, teammate_name) or did_work
        owner._inbox_cursor[(team_name, teammate_name)] = cursor + rows_processed
        return did_work

    def _claim_board_task_to_work_item(self, team_name: str, teammate_name: str) -> bool:
        owner = self.owner
        claimed = owner.claim_next_board_task(team_name, owner=teammate_name)
        if not claimed:
            return False
        task_id = str(claimed.get("id"))
        subject = str(claimed.get("subject") or f"task-{task_id}")
        description = str(claimed.get("description") or "").strip()
        instruction = description or subject
        if owner._teammate_requires_plan_approval(team_name, teammate_name):
            entry = owner.approval_service.create_request(team_name, teammate_name, task_id, subject)
            owner._persist_approval(entry)
            owner._emit(
                team_name,
                EVENT_PLAN_APPROVAL_REQUESTED,
                {
                    "request_id": entry["request_id"],
                    "teammate": teammate_name,
                    "task_id": task_id,
                    "status": "pending",
                },
            )
            return True
        item = owner.store.create_work_item(
            team_name,
            owner=teammate_name,
            title=subject,
            instruction=instruction,
            payload={"board_task_id": task_id},
        )
        owner._emit(
            team_name,
            EVENT_WORK_ITEM_ASSIGNED,
            {"work_id": item["work_id"], "owner": teammate_name, "status": item["status"]},
        )
        return True

    def _enqueue_message_work(self, team_name: str, teammate_name: str, message_row: Dict[str, Any]) -> bool:
        owner = self.owner
        message_id = str(message_row.get("message_id") or "").strip()
        if not message_id:
            return False
        text = str(message_row.get("text") or "").strip()
        if not text:
            return False
        existing = owner.store.list_work_items(team_name, owner=teammate_name)
        for item in existing:
            payload = item.get("payload") if isinstance(item.get("payload"), dict) else {}
            if str(payload.get("source_message_id") or "") == message_id:
                return False

        summary = str(message_row.get("summary") or "").strip()
        message_kind = str(message_row.get("type") or MESSAGE_TYPE_MESSAGE).strip().lower()
        title = summary or f"message-{message_id[:8]}"
        payload = {
            "source": "message",
            "source_message_id": message_id,
            "message_type": message_kind,
            "from_member": str(message_row.get("from") or ""),
            "request_id": str(message_row.get("request_id") or ""),
        }
        item = owner.store.create_work_item(
            team_name,
            owner=teammate_name,
            title=title,
            instruction=text,
            payload=payload,
        )
        owner._emit(
            team_name,
            EVENT_WORK_ITEM_ASSIGNED,
            {"work_id": item["work_id"], "owner": teammate_name, "status": item["status"]},
        )
        return True

    def _apply_plan_approval_response(self, team_name: str, teammate_name: str, message_row: Dict[str, Any]) -> None:
        owner = self.owner
        request_id = str(message_row.get("request_id") or "").strip()
        if not request_id:
            return
        approved_raw = message_row.get("approved")
        if isinstance(approved_raw, bool):
            approved = approved_raw
        else:
            text = str(message_row.get("text") or "").lower()
            approved = "approve" in text and "reject" not in text
        feedback = str(message_row.get("feedback") or "").strip()
        updated = owner.approval_service.apply_response(team_name, teammate_name, request_id, approved, feedback)
        if updated:
            owner._persist_approval(owner.approval_service.get_request(request_id))

