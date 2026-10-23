"""Session store maintenance command: audit, repair, migrate."""
from __future__ import annotations

import json

from core.session_log import SessionLog
from core.session_manager import SessionManager
from ridgecode.maintenance import run


def _snapshot(*messages: tuple[str, str]) -> dict:
    return {
        "version": 2,
        "schema_version": 2,
        "history_messages": [
            {"role": role, "content": content, "message_id": f"m{index}"}
            for index, (role, content) in enumerate(messages, start=1)
        ],
    }


def _lagging_store(tmp_path) -> str:
    manager = SessionManager(sessions_dir=str(tmp_path))
    session_id = manager.create_session("lagging")
    manager.save_session(session_id, _snapshot(("user", "one")))
    (tmp_path / f"{session_id}.json").write_text(
        json.dumps(_snapshot(("user", "one"), ("assistant", "two"))), encoding="utf-8",
    )
    return session_id


def test_dry_run_reports_without_changing_files(tmp_path):
    session_id = _lagging_store(tmp_path)
    before = SessionLog(tmp_path, session_id).path.read_bytes()

    lines: list[str] = []
    result = run(str(tmp_path), dry_run=True, output=lines.append)

    assert result["exists"] is True
    assert result["sessions"] == 1
    assert result["states"] == {"log_behind": 1}
    assert result["dry_run"] is True
    assert SessionLog(tmp_path, session_id).path.read_bytes() == before
    assert any("dry run" in line for line in lines)


def test_apply_repairs_and_reports(tmp_path):
    session_id = _lagging_store(tmp_path)
    lines: list[str] = []

    result = run(str(tmp_path), output=lines.append)

    assert result["repair"] == {"repaired": 1}
    assert SessionLog(tmp_path, session_id).derive_messages()[-1]["content"] == "two"
    assert SessionManager(sessions_dir=str(tmp_path)).audit_session(session_id)["state"] == "ok"
    assert any(line.strip().startswith("repair:") for line in lines)


def test_apply_migrates_legacy_snapshots(tmp_path):
    legacy = tmp_path / "abcdefabcdef.json"
    legacy.write_text(json.dumps({
        "version": 1,
        "history_messages": [{"role": "user", "content": "old"}],
    }), encoding="utf-8")

    result = run(str(tmp_path))

    assert result["migration"] == {"migrated": 1}
    assert json.loads(legacy.read_text(encoding="utf-8"))["schema_version"] == 2


def test_missing_store_is_reported_cleanly(tmp_path):
    lines: list[str] = []
    result = run(str(tmp_path / "nope"), output=lines.append)
    assert result["exists"] is False
    assert result["sessions"] == 0
    assert "not found" in lines[0]


def test_prune_empty_dry_run_lists_without_deleting(tmp_path):
    manager = SessionManager(sessions_dir=str(tmp_path))
    empty = manager.create_session("empty")
    kept = manager.create_session("kept")
    manager.save_session(kept, _snapshot(("user", "hello")))

    lines: list[str] = []
    result = run(str(tmp_path), dry_run=True, prune_empty=True, output=lines.append)

    assert result["empty"] == [empty]
    assert result["pruned"] == []
    assert (tmp_path / f"{empty}.json").exists()
    assert any("empty sessions: 1" in line for line in lines)


def test_prune_empty_deletes_only_empty_sessions(tmp_path):
    manager = SessionManager(sessions_dir=str(tmp_path))
    empty = manager.create_session("empty")
    kept = manager.create_session("kept")
    manager.save_session(kept, _snapshot(("user", "hello")))

    result = run(str(tmp_path), prune_empty=True)

    assert result["pruned"] == [empty]
    assert not (tmp_path / f"{empty}.json").exists()
    assert not (tmp_path / f"{empty}.jsonl").exists()
    assert (tmp_path / f"{kept}.json").exists()
    assert SessionManager(sessions_dir=str(tmp_path)).get_session(empty) is None


def test_prune_empty_skips_sessions_with_an_open_turn(tmp_path):
    manager = SessionManager(sessions_dir=str(tmp_path))
    active = manager.create_session("active")
    (tmp_path / f"{active}.turn.json").write_text("{}", encoding="utf-8")

    result = run(str(tmp_path), prune_empty=True)

    assert result["pruned"] == []
    assert (tmp_path / f"{active}.json").exists()
    assert (tmp_path / f"{active}.turn.json").exists()


def test_cli_json_output_is_machine_readable(tmp_path, capsys):
    from ridgecode.maintenance import main

    _lagging_store(tmp_path)
    assert main(["--sessions-dir", str(tmp_path), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["states"] == {"log_behind": 1}
    assert payload["repair"] == {"repaired": 1}
