"""Aggregate real compaction traces (no provider calls).

Session traces are JSONL. This reads them and reports what actually happened:
how often pruning fired, whether summarization triggered, why it was skipped,
and how large the context was at the trigger point.

    python -m scripts.context_trace_report
    python -m scripts.context_trace_report --dir memory/traces --json
"""
from __future__ import annotations

import argparse
import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class TraceReport:
    root: str
    files: int = 0
    lines: int = 0
    malformed: int = 0
    sessions: int = 0
    prune_events: int = 0
    pruned_messages: int = 0
    triggered: int = 0
    completed: int = 0
    skipped: int = 0
    summaries: int = 0
    messages_compressed: int = 0
    summary_chars: int = 0
    estimated_tokens_total: int = 0
    threshold_total: int = 0
    sessions_with_compaction: int = 0
    skip_reasons: dict[str, int] = field(default_factory=dict)

    @property
    def avg_estimated_tokens(self) -> int:
        return self.estimated_tokens_total // self.triggered if self.triggered else 0

    @property
    def avg_threshold(self) -> int:
        return self.threshold_total // self.triggered if self.triggered else 0

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["avg_estimated_tokens"] = self.avg_estimated_tokens
        data["avg_threshold"] = self.avg_threshold
        return data


def _payload(event: dict[str, Any]) -> dict[str, Any]:
    payload = event.get("payload")
    return payload if isinstance(payload, dict) else {}


def scan(root: str | Path) -> TraceReport:
    root = Path(root)
    report = TraceReport(root=str(root))
    if not root.is_dir():
        return report
    seen_sessions: set[str] = set()
    compacted_sessions: set[str] = set()
    for path in sorted(root.glob("*.jsonl")):
        report.files += 1
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for line in text.splitlines():
            if not line.strip():
                continue
            report.lines += 1
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                report.malformed += 1
                continue
            if not isinstance(event, dict):
                report.malformed += 1
                continue
            name = str(event.get("event") or "")
            session = str(event.get("session_id") or path.stem)
            seen_sessions.add(session)
            payload = _payload(event)

            if name == "history_tool_outputs_pruned":
                report.prune_events += 1
                try:
                    report.pruned_messages += int(payload.get("messages") or 0)
                except (TypeError, ValueError):
                    pass
            elif name == "history_compression_triggered":
                report.triggered += 1
                try:
                    report.estimated_tokens_total += int(payload.get("estimated_tokens") or 0)
                    report.threshold_total += int(payload.get("threshold") or 0)
                except (TypeError, ValueError):
                    pass
            elif name == "history_compression_completed":
                report.completed += 1
                compacted_sessions.add(session)
                if payload.get("summary_generated"):
                    report.summaries += 1
                try:
                    report.messages_compressed += int(payload.get("messages_compressed") or 0)
                except (TypeError, ValueError):
                    pass
                details = payload.get("details")
                if isinstance(details, dict):
                    try:
                        report.summary_chars += int(details.get("summary_len") or 0)
                    except (TypeError, ValueError):
                        pass
            elif name == "history_compression_skipped":
                report.skipped += 1
                reason = str(payload.get("reason") or "unknown")
                report.skip_reasons[reason] = report.skip_reasons.get(reason, 0) + 1

    report.sessions = len(seen_sessions)
    report.sessions_with_compaction = len(compacted_sessions)
    return report


def render(report: TraceReport) -> str:
    reasons = ", ".join(
        f"{name}={count}" for name, count in sorted(
            report.skip_reasons.items(), key=lambda item: item[1], reverse=True,
        )
    ) or "none"
    return "\n".join([
        f"Traces: {report.root}",
        f"  files={report.files} lines={report.lines} malformed={report.malformed} "
        f"sessions={report.sessions}",
        f"  prune events={report.prune_events} pruned messages={report.pruned_messages}",
        f"  compression triggered={report.triggered} completed={report.completed} "
        f"skipped={report.skipped}",
        f"  sessions with compaction={report.sessions_with_compaction} "
        f"summaries={report.summaries} summary chars={report.summary_chars}",
        f"  avg tokens at trigger={report.avg_estimated_tokens} "
        f"avg threshold={report.avg_threshold}",
        f"  skip reasons: {reasons}",
    ])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="context_trace_report", description=__doc__)
    parser.add_argument("--dir", default="memory/traces")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    report = scan(args.dir)
    print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2) if args.json else render(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
