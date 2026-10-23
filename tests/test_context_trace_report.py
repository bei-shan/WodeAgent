"""Trace-based compaction report."""
from __future__ import annotations

import json

from scripts.context_trace_report import main, render, scan


def _write(path, events):
    lines = [json.dumps(event, ensure_ascii=False) for event in events]
    path.write_text("\n".join(lines) + "\n{not json\n", encoding="utf-8")


def test_scan_aggregates_events(tmp_path):
    session = "s-1"
    _write(tmp_path / "a.jsonl", [
        {"session_id": session, "event": "history_tool_outputs_pruned", "payload": {"messages": 5}},
        {"session_id": session, "event": "history_compression_triggered",
         "payload": {"estimated_tokens": 8000, "threshold": 6000}},
        {"session_id": session, "event": "history_compression_completed",
         "payload": {"messages_compressed": 12, "summary_generated": True,
                     "details": {"summary_len": 900}}},
        {"session_id": session, "event": "history_compression_skipped",
         "payload": {"reason": "rounds_not_enough"}},
    ])
    _write(tmp_path / "b.jsonl", [
        {"session_id": "s-2", "event": "history_compression_skipped",
         "payload": {"reason": "summary_generation_failed"}},
    ])

    report = scan(tmp_path)
    assert report.files == 2
    assert report.malformed == 2
    assert report.sessions == 2
    assert report.prune_events == 1 and report.pruned_messages == 5
    assert report.triggered == 1 and report.completed == 1 and report.skipped == 2
    assert report.summaries == 1 and report.summary_chars == 900
    assert report.sessions_with_compaction == 1
    assert report.avg_estimated_tokens == 8000
    assert report.skip_reasons == {"rounds_not_enough": 1, "summary_generation_failed": 1}


def test_missing_directory_is_empty(tmp_path):
    report = scan(tmp_path / "nope")
    assert report.files == 0 and report.triggered == 0


def test_render_and_json_cli(tmp_path, capsys):
    _write(tmp_path / "a.jsonl", [
        {"session_id": "s", "event": "history_compression_triggered",
         "payload": {"estimated_tokens": 100, "threshold": 80}},
    ])
    assert main(["--dir", str(tmp_path), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["triggered"] == 1

    assert main(["--dir", str(tmp_path)]) == 0
    text = capsys.readouterr().out
    assert "compression triggered=1" in text
    assert render(scan(tmp_path)).startswith("Traces:")
