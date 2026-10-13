"""Team checkpoint projection: serialize/restore across independent owners.

TeamStore owns durable teams/work items; WorkerSupervisor owns live workers;
ApprovalService owns approval requests; this service only projects/checkpoints.
No TeamManager import, worker startup or tool execution logic lives here.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Protocol


class TeamCheckpointHost(Protocol):
    store: Any
    worker_supervisor: Any
    approval_service: Any
    _processed_by_member: dict
    _inbox_cursor: dict

    def get_status(self, team_name: str) -> dict: ...
    def collect_work(self, team_name: str) -> dict: ...
    def list_plan_approvals(self, team_name: str) -> list[dict]: ...
    def list_board_tasks(self, team_name: str) -> list[dict]: ...
    def _persist_approval(self, request: dict) -> None: ...


class TeamCheckpointService:
    def __init__(self, owner: TeamCheckpointHost):
        self.owner = owner

    def snapshot(self) -> Dict[str, Any]:
        owner = self.owner
        teams = owner.store.list_teams()
        team_states: Dict[str, Dict[str, Any]] = {}
        work_counts: Dict[str, Dict[str, Any]] = {}
        approval_counts: Dict[str, Dict[str, Any]] = {}
        task_board_counts: Dict[str, Dict[str, Any]] = {}
        for name in teams:
            team_state = owner.get_status(name)
            worker_state = owner.worker_supervisor.team_state(name)
            team_state.update(worker_state)
            team_states[name] = team_state
            work_counts[name] = owner.collect_work(name).get("counts", {})
            all_reqs = owner.list_plan_approvals(name)
            pending_reqs = [req for req in all_reqs if req.get("status") == "pending"]
            approval_counts[name] = {
                "pending": len(pending_reqs),
                "approved": len([req for req in all_reqs if req.get("status") == "approved"]),
                "rejected": len([req for req in all_reqs if req.get("status") == "rejected"]),
                "requests": all_reqs,
            }
            board_rows = owner.list_board_tasks(name)
            blocked = 0
            pending = 0
            in_progress = 0
            for row in board_rows:
                status = str(row.get("status") or "")
                blocked_by = row.get("blocked_by")
                if status == "pending":
                    pending += 1
                    if isinstance(blocked_by, list) and blocked_by:
                        blocked += 1
                elif status == "in_progress":
                    in_progress += 1
            task_board_counts[name] = {
                "total": len(board_rows),
                "blocked": blocked,
                "pending": pending,
                "in_progress": in_progress,
            }
        processed_messages: Dict[str, List[str]] = {}
        for (team, member), msg_ids in owner._processed_by_member.items():
            key = f"{team}|{member}"
            processed_messages[key] = list(msg_ids)

        inbox_cursors: Dict[str, int] = {}
        for (team, member), cursor in getattr(owner, "_inbox_cursor", {}).items():
            key = f"{team}|{member}"
            inbox_cursors[key] = cursor

        return {
            "teams": team_states,
            "work_items": work_counts,
            "approvals": approval_counts,
            "task_board": task_board_counts,
            "processed_messages": processed_messages,
            "inbox_cursors": inbox_cursors,
        }

    def restore(self, state: Optional[Dict[str, Any]]) -> None:
        owner = self.owner
        snapshot = state or {}
        names = set(owner.store.list_teams())
        snapshot_teams = snapshot.get("teams")
        if isinstance(snapshot_teams, dict):
            names.update(snapshot_teams.keys())
        for team_name in sorted(names):
            try:
                cfg = owner.store.read_team(team_name)
            except FileNotFoundError:
                continue
            owner.store.requeue_running_work_items(team_name)
        # Restore processed message IDs from snapshot.
        snapshot_processed = snapshot.get("processed_messages")
        if isinstance(snapshot_processed, dict):
            for key, msg_ids in snapshot_processed.items():
                if "|" in key and isinstance(msg_ids, list):
                    team, member = key.split("|", 1)
                    owner._processed_by_member[(team, member)] = set(msg_ids)
        # Restore inbox cursors from snapshot.
        snapshot_cursors = snapshot.get("inbox_cursors")
        if isinstance(snapshot_cursors, dict):
            if not hasattr(owner, "_inbox_cursor"):
                owner._inbox_cursor: Dict[tuple[str, str], int] = {}
            for key, cursor in snapshot_cursors.items():
                if "|" in key and isinstance(cursor, int):
                    team, member = key.split("|", 1)
                    owner._inbox_cursor[(team, member)] = cursor
        # Restore pending approval requests from snapshot.
        snapshot_approvals = snapshot.get("approvals")
        if isinstance(snapshot_approvals, dict):
            for team, data in snapshot_approvals.items():
                if not isinstance(data, dict):
                    continue
                requests = data.get("requests")
                if not isinstance(requests, list):
                    continue
                for req in requests:
                    if not isinstance(req, dict):
                        continue
                    req_id = str(req.get("request_id") or "").strip()
                    if not req_id or req_id in owner.approval_service._requests:
                        continue
                    restored = owner.approval_service.upsert_request(dict(req))
                    owner._persist_approval(restored)

