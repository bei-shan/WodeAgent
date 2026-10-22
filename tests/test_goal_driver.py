"""Goal round driver: deciding and running automatic continuation rounds."""
from __future__ import annotations

import logging
from pathlib import Path
from threading import Event
from types import SimpleNamespace

import pytest

from core.goal_driver import CONTINUATION_PROMPT, GoalRoundDriver
from core.goals import GoalStore
from core.runtime.turn_lifecycle import AgentTurnLifecycle

SESSION = "abc123def456"


def _store(tmp_path) -> GoalStore:
    return GoalStore(Path(tmp_path) / "goals")


# ---------------------------------------------------------------------------
# Decisions
# ---------------------------------------------------------------------------
def test_no_goal_means_no_continuation(tmp_path):
    driver = GoalRoundDriver(_store(tmp_path))
    assert driver.decide(SESSION).continue_run is False


def test_active_goal_claims_a_round_and_returns_a_prompt(tmp_path):
    store = _store(tmp_path)
    store.set_objective(SESSION, "Ship the migration", max_rounds=3)
    driver = GoalRoundDriver(store)

    decision = driver.decide(SESSION)
    assert decision.continue_run is True
    assert "Ship the migration" in decision.prompt
    assert decision.prompt == CONTINUATION_PROMPT.format(objective="Ship the migration")
    assert store.get(SESSION).rounds_started == 1


@pytest.mark.parametrize("status_change", ["complete", "block"])
def test_terminal_goals_never_continue(tmp_path, status_change):
    store = _store(tmp_path)
    store.set_objective(SESSION, "x")
    if status_change == "complete":
        store.complete(SESSION)
    else:
        store.block(SESSION, "waiting on upstream")
    decision = GoalRoundDriver(store).decide(SESSION)
    assert decision.continue_run is False
    assert status_change in decision.reason


def test_budget_cap_and_interrupts_stop_the_driver(tmp_path):
    store = _store(tmp_path)
    store.set_objective(SESSION, "x", max_rounds=2)
    driver = GoalRoundDriver(store)

    assert driver.decide(SESSION).continue_run
    assert driver.decide(SESSION).continue_run
    exhausted = driver.decide(SESSION)
    assert exhausted.continue_run is False
    assert "budget" in exhausted.reason

    store.set_objective(SESSION, "y", max_rounds=5)
    assert driver.decide(SESSION, interrupted=True).continue_run is False
    assert driver.decide(SESSION, turn_status="failed").continue_run is False
    assert driver.decide(SESSION, turn_status="cancelled").continue_run is False


def test_per_invocation_cap_and_disabled_switch(tmp_path):
    store = _store(tmp_path)
    store.set_objective(SESSION, "x", max_rounds=10)
    capped = GoalRoundDriver(store, max_auto_rounds=0).decide(SESSION)
    assert capped.continue_run is False and "cap" in capped.reason

    disabled = GoalRoundDriver(store, enabled=False).decide(SESSION)
    assert disabled.continue_run is False

    assert GoalRoundDriver(store).decide("not-a-session").continue_run is False


# ---------------------------------------------------------------------------
# Turn lifecycle integration
# ---------------------------------------------------------------------------
class _History:
    def __init__(self):
        self.users = []

    def append_user(self, content):
        self.users.append(content)

    def get_message_count(self):
        return len(self.users)

    def get_rounds_count(self):
        return len(self.users)


class _Trace:
    def log_event(self, *args, **kwargs):
        pass


class _Host:
    def __init__(self, driver, *, mode="ok", on_round=None):
        self.logger = logging.getLogger("goal-driver-test")
        self.console_verbose = False
        self.console_progress = False
        self.context_builder = SimpleNamespace(set_skills_prompt=lambda _: None)
        self._skills_prompt = ""
        self.trace_logger = _Trace()
        self.history_manager = _History()
        self.event_sink = SimpleNamespace(emit=lambda *_: None)
        self._run_id = 0
        self._run_limit_hit = False
        self._interrupt_requested = Event()
        self._session_id = SESSION
        self._goal_driver = driver
        self.mode = mode
        self.rounds = 0
        self.on_round = on_round

    def _refresh_skills_prompt(self):
        pass

    def _log_system_messages_if_needed(self, trace):
        pass

    def _log_message_write(self, trace, role, content, metadata, step):
        pass

    def _auto_save_session(self):
        pass

    def _console(self, message):
        pass

    def _react_loop(self, pending_input, show_raw, trace_logger):
        if self.mode == "error":
            raise RuntimeError("provider down")
        self.rounds += 1
        if self.on_round is not None:
            self.on_round(self.rounds)
        return f"round:{self.rounds}"


def test_lifecycle_runs_continuations_until_budget_is_spent(tmp_path):
    store = _store(tmp_path)
    store.set_objective(SESSION, "Polish the docs", max_rounds=2)
    host = _Host(GoalRoundDriver(store))

    result = AgentTurnLifecycle(host).run("start")
    assert host.rounds == 3  # initial turn + two automatic rounds
    assert result == "round:3"
    assert host.history_manager.users[0] == "start"
    assert "Continue working toward the active goal" in host.history_manager.users[1]
    assert store.get(SESSION).rounds_started == 2

    host.rounds = 0
    AgentTurnLifecycle(host).run("again")
    assert host.rounds == 1  # budget exhausted, no continuation


def test_lifecycle_stops_when_the_goal_completes_mid_run(tmp_path):
    store = _store(tmp_path)

    def complete_after_first(round_number):
        # The initial turn is round 1; complete the goal inside the continuation.
        if round_number == 2:
            store.complete(SESSION)

    store.set_objective(SESSION, "one round only", max_rounds=5)
    host = _Host(GoalRoundDriver(store), on_round=complete_after_first)

    AgentTurnLifecycle(host).run("start")
    assert host.rounds == 2  # initial + exactly one continuation


def test_failed_turn_does_not_continue(tmp_path):
    store = _store(tmp_path)
    store.set_objective(SESSION, "x", max_rounds=3)
    host = _Host(GoalRoundDriver(store), mode="error")

    with pytest.raises(RuntimeError):
        AgentTurnLifecycle(host).run("start")
    assert host.rounds == 0
    assert store.get(SESSION).rounds_started == 0


def test_no_driver_means_single_turn(tmp_path):
    host = _Host(None)
    AgentTurnLifecycle(host).run("start")
    assert host.rounds == 1
