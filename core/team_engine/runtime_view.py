"""Team runtime view — formats team state for injection into the system prompt.

Extracted from CodeAgent._format_runtime_system_blocks (P1 #8).
"""

from __future__ import annotations

from typing import Any


class TeamRuntimeView:
    """格式化为 system prompt 注入的团队运行时摘要。"""

    @staticmethod
    def format(
        events: list[dict],
        runtime_state: dict | None = None,
        max_lines: int = 16,
    ) -> list[str]:
        has_events = bool(events)
        state = runtime_state if isinstance(runtime_state, dict) else {}
        teams = state.get("teams") if isinstance(state.get("teams"), dict) else {}
        work_items = state.get("work_items") if isinstance(state.get("work_items"), dict) else {}
        approvals = state.get("approvals") if isinstance(state.get("approvals"), dict) else {}
        task_board = state.get("task_board") if isinstance(state.get("task_board"), dict) else {}
        if not has_events and not work_items:
            return []
        lines = ["[Team Runtime]"]

        for team_name in sorted(work_items.keys()):
            counts = work_items.get(team_name)
            if not isinstance(counts, dict):
                continue
            queued = int(counts.get("queued", 0) or 0)
            running = int(counts.get("running", 0) or 0)
            succeeded = int(counts.get("succeeded", 0) or 0)
            failed = int(counts.get("failed", 0) or 0)
            lines.append(
                f"- {team_name} work queued={queued} running={running} succeeded={succeeded} failed={failed}"
            )
            team_state = teams.get(team_name) if isinstance(teams, dict) else {}
            if isinstance(team_state, dict):
                idle_members = team_state.get("idle_teammates")
                active_members = team_state.get("active_teammates")
                if isinstance(idle_members, list) or isinstance(active_members, list):
                    idle_count = len(idle_members) if isinstance(idle_members, list) else 0
                    active_count = len(active_members) if isinstance(active_members, list) else 0
                    lines.append(f"- {team_name} teammates active={active_count} idle={idle_count}")
                last_error = str(team_state.get("last_error") or "").strip()
                if last_error:
                    compact_error = " ".join(last_error.split())
                    if len(compact_error) > 120:
                        compact_error = f"{compact_error[:117]}..."
                    lines.append(f"- {team_name} last_error={compact_error}")
            approval_counts = approvals.get(team_name) if isinstance(approvals, dict) else {}
            if isinstance(approval_counts, dict):
                pending = int(approval_counts.get("pending", 0) or 0)
                approved = int(approval_counts.get("approved", 0) or 0)
                rejected = int(approval_counts.get("rejected", 0) or 0)
                if pending or approved or rejected:
                    lines.append(
                        f"- {team_name} approvals pending={pending} approved={approved} rejected={rejected}"
                    )
            board_counts = task_board.get(team_name) if isinstance(task_board, dict) else {}
            if isinstance(board_counts, dict):
                blocked = int(board_counts.get("blocked", 0) or 0)
                pending_tasks = int(board_counts.get("pending", 0) or 0)
                in_progress = int(board_counts.get("in_progress", 0) or 0)
                if blocked or pending_tasks or in_progress:
                    lines.append(
                        f"- {team_name} tasks blocked={blocked} pending={pending_tasks} in_progress={in_progress}"
                    )

        deduped_events: list[dict] = []
        seen: set[tuple[str, str, str, str, str]] = set()
        for event in events:
            if not isinstance(event, dict):
                continue
            team = str(event.get("team", "unknown"))
            event_type = str(event.get("type", "event"))
            payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
            signature = (
                team, event_type,
                str(payload.get("message_id") or ""),
                str(payload.get("work_id") or ""),
                str(payload.get("status") or ""),
            )
            if signature in seen:
                continue
            seen.add(signature)
            deduped_events.append(event)

        max_event_lines = 8
        for event in deduped_events[:max_event_lines]:
            team = event.get("team", "unknown")
            event_type = event.get("type", "event")
            payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
            status = payload.get("status")
            message_id = payload.get("message_id")
            work_id = payload.get("work_id")
            if work_id and status:
                lines.append(f"- event {team}:{event_type} work={work_id} status={status}")
            elif message_id and status:
                lines.append(f"- event {team}:{event_type} message={message_id} status={status}")
            elif message_id:
                lines.append(f"- event {team}:{event_type} message={message_id}")
            else:
                lines.append(f"- event {team}:{event_type}")
        if len(deduped_events) > max_event_lines:
            lines.append(f"- ... {len(deduped_events) - max_event_lines} more events")

        limit = max(2, int(max_lines or 0))
        if len(lines) > limit:
            hidden = len(lines) - (limit - 1)
            lines = lines[: limit - 1] + [f"- ... {hidden} more lines"]

        return ["\n".join(lines)]
