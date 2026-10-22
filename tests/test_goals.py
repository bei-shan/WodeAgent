"""Session-scoped goals: state machine, persistence and the Goal tool."""
from __future__ import annotations

import json

import pytest

from core.goals import ACTIVE, BLOCKED, COMPLETE, GoalError, GoalStore
from tools.builtin.goal import GoalTool

SESSION = "abc123def456"


class _Agent:
    _session_id = SESSION


def _tool(tmp_path) -> GoalTool:
    return GoalTool(project_root=str(tmp_path), code_agent=_Agent())


def test_set_and_get_persist(tmp_path):
    store = GoalStore(tmp_path / "goals")
    goal = store.set_objective(SESSION, "Ship the migration", max_rounds=3)
    assert goal.status == ACTIVE
    assert goal.max_rounds == 3

    reread = GoalStore(tmp_path / "goals").get(SESSION)
    assert reread is not None
    assert reread.objective == "Ship the migration"
    assert reread.rounds_started == 0


def test_objective_is_required_and_normalised(tmp_path):
    store = GoalStore(tmp_path / "goals")
    with pytest.raises(GoalError):
        store.set_objective(SESSION, "   ")
    goal = store.set_objective(SESSION, "  fix\n the\tlogin  ")
    assert goal.objective == "fix the login"


def test_round_budget_is_enforced(tmp_path):
    store = GoalStore(tmp_path / "goals")
    store.set_objective(SESSION, "two rounds", max_rounds=2)
    assert store.start_round(SESSION).rounds_started == 1
    assert store.start_round(SESSION).rounds_started == 2
    with pytest.raises(GoalError):
        store.start_round(SESSION)


def test_complete_and_block_transitions(tmp_path):
    store = GoalStore(tmp_path / "goals")
    store.set_objective(SESSION, "do it")
    assert store.complete(SESSION).status == COMPLETE
    with pytest.raises(GoalError):
        store.start_round(SESSION)

    store.set_objective(SESSION, "another")
    with pytest.raises(GoalError):
        store.block(SESSION, "  ")
    blocked = store.block(SESSION, "waiting on upstream API")
    assert blocked.status == BLOCKED
    assert blocked.blocked_reason == "waiting on upstream API"


def test_clear_and_missing_goal(tmp_path):
    store = GoalStore(tmp_path / "goals")
    assert store.clear(SESSION) is False
    with pytest.raises(GoalError):
        store.start_round(SESSION)
    store.set_objective(SESSION, "x")
    assert store.clear(SESSION) is True
    assert store.get(SESSION) is None


def test_invalid_session_id_is_rejected(tmp_path):
    store = GoalStore(tmp_path / "goals")
    with pytest.raises(GoalError):
        store.get("not-a-session")


def test_goal_tool_actions(tmp_path):
    tool = _tool(tmp_path)

    payload = json.loads(tool.run({"action": "set", "objective": "Ship it", "max_rounds": 2}))
    assert payload["status"] == "success"
    assert payload["data"]["goal"]["objective"] == "Ship it"

    payload = json.loads(tool.run({"action": "start_round"}))
    assert payload["data"]["goal"]["rounds_started"] == 1
    assert "rounds: 1/2" in payload["text"]

    payload = json.loads(tool.run({"action": "block", "reason": "needs review"}))
    assert payload["data"]["goal"]["status"] == BLOCKED

    payload = json.loads(tool.run({"action": "get"}))
    assert payload["data"]["goal"]["blocked_reason"] == "needs review"

    payload = json.loads(tool.run({"action": "clear"}))
    assert payload["data"]["cleared"] is True
    assert json.loads(tool.run({"action": "get"}))["data"]["goal"] is None


def test_goal_tool_rejects_bad_action_and_missing_session(tmp_path):
    tool = _tool(tmp_path)
    payload = json.loads(tool.run({"action": "explode"}))
    assert payload["error"]["code"] == "INVALID_PARAM"

    unbound = GoalTool(project_root=str(tmp_path), code_agent=object())
    payload = json.loads(unbound.run({"action": "get"}))
    assert payload["status"] == "error"
    assert "session" in payload["error"]["message"].lower()


def test_goal_tool_without_goal_reports_cleanly(tmp_path):
    tool = _tool(tmp_path)
    payload = json.loads(tool.run({"action": "get"}))
    assert payload["status"] == "success"
    assert payload["data"]["goal"] is None
    assert "No goal" in payload["text"]
