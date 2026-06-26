"""AgentTeams worker retry tests."""

from __future__ import annotations

import threading

import pytest

from core.team_engine.execution import ExecutionService


class RetryableRateLimitError(Exception):
    status_code = 429


class NonRetryableError(Exception):
    status_code = 400


def _service():
    return ExecutionService(
        project_root=".",
        llm=object(),
        tool_registry=object(),
        work_executor=None,
        read_team_fn=lambda team: {"members": [{"name": "dev1"}]},
    )


def test_worker_retries_retryable_exception_then_succeeds(monkeypatch):
    service = _service()
    attempts = {"count": 0}
    monkeypatch.setenv("TEAM_LLM_MAX_RETRIES", "2")
    monkeypatch.setenv("TEAM_LLM_RETRY_BACKOFF", "0")

    def fake_run(team_name, teammate_name, work_item):
        attempts["count"] += 1
        if attempts["count"] < 3:
            raise RetryableRateLimitError("rate limited")
        return {"result": "ok"}

    monkeypatch.setattr(service, "_run_turn_executor_work", fake_run)

    result = service.execute_work_item("demo", "dev1", {"title": "work"})

    assert result == {"result": "ok"}
    assert attempts["count"] == 3


def test_worker_non_retryable_exception_fails_fast(monkeypatch):
    service = _service()
    attempts = {"count": 0}
    monkeypatch.setenv("TEAM_LLM_MAX_RETRIES", "2")

    def fake_run(team_name, teammate_name, work_item):
        attempts["count"] += 1
        raise NonRetryableError("bad request")

    monkeypatch.setattr(service, "_run_turn_executor_work", fake_run)

    with pytest.raises(RuntimeError):
        service.execute_work_item("demo", "dev1", {"title": "work"})

    assert attempts["count"] == 1


def test_worker_releases_semaphore_before_backoff(monkeypatch):
    semaphore = threading.Semaphore(1)
    service = ExecutionService(
        project_root=".",
        llm=object(),
        tool_registry=object(),
        work_executor=None,
        read_team_fn=lambda team: {"members": [{"name": "dev1"}]},
        llm_semaphore=semaphore,
    )
    attempts = {"count": 0}
    observed_released = []
    monkeypatch.setenv("TEAM_LLM_MAX_RETRIES", "1")
    monkeypatch.setenv("TEAM_LLM_RETRY_BACKOFF", "0.1")

    def fake_run(team_name, teammate_name, work_item):
        attempts["count"] += 1
        if attempts["count"] == 1:
            raise RetryableRateLimitError("rate limited")
        return {"result": "ok"}

    def fake_sleep(seconds):
        acquired = semaphore.acquire(blocking=False)
        observed_released.append(acquired)
        if acquired:
            semaphore.release()

    monkeypatch.setattr(service, "_run_turn_executor_work", fake_run)
    monkeypatch.setattr("core.team_engine.execution.time.sleep", fake_sleep)
    monkeypatch.setattr("core.team_engine.execution.random.uniform", lambda a, b: 0)

    result = service.execute_work_item("demo", "dev1", {"title": "work"})

    assert result == {"result": "ok"}
    assert observed_released == [True]
