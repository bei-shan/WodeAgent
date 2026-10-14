"""Turn outcome durability under concurrent writers and I/O failures."""
from __future__ import annotations

import json
import os
import threading

import pytest

from core.runtime.turn_state import TurnOutcome, TurnStatus, load_turn_outcome, persist_turn_outcome
from core.session_manager import SessionManager


SID = "123456789abc"


def test_turn_outcome_atomic_concurrent_writers_never_leave_partial_json(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    expected = {f"response-{index}" for index in range(12)}
    start = threading.Barrier(len(expected))
    errors = []

    def save(index):
        try:
            start.wait(timeout=3)
            persist_turn_outcome(
                SID, TurnOutcome(TurnStatus.COMPLETED, response=f"response-{index}")
            )
            read = load_turn_outcome(SID)
            assert read is not None and read.response in expected
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=save, args=(i,)) for i in range(12)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=8)
        assert not thread.is_alive()
    assert not errors, errors
    data = json.loads((SessionManager()._dir / f"{SID}.turn.json").read_text())
    assert data["response"] in expected
    assert not list(SessionManager()._dir.glob(f".{SID}.*.turn.tmp"))


def test_failed_atomic_replace_keeps_last_good_outcome(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    persist_turn_outcome(SID, TurnOutcome(TurnStatus.COMPLETED, response="old"))
    existing = SessionManager()._dir / f"{SID}.turn.json"

    def fail_replace(source, target):
        assert os.path.isfile(source)
        assert str(target) == str(existing)
        raise OSError("simulated disk failure")

    with monkeypatch.context() as patcher:
        patcher.setattr(os, "replace", fail_replace)
        with pytest.raises(OSError, match="disk failure"):
            persist_turn_outcome(SID, TurnOutcome(TurnStatus.FAILED))

    restored = load_turn_outcome(SID)
    assert restored is not None
    assert restored.status is TurnStatus.COMPLETED
    assert restored.response == "old"
    assert not list(SessionManager()._dir.glob(f".{SID}.*.turn.tmp"))


def test_turn_state_rejects_invalid_session_identifier(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    for bad in ("../../private", "abcd", "a" * 13, "g" * 12):
        with pytest.raises(ValueError):
            persist_turn_outcome(bad, TurnOutcome(TurnStatus.COMPLETED))
        assert load_turn_outcome(bad) is None
