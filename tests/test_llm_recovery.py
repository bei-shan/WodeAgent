"""Model retry boundaries: no unsafe tool replay or partial-stream duplicates."""
from types import SimpleNamespace

import pytest

from core.llm import HelloAgentsLLM
from core.llm_recovery import ModelInvocationError, classify_model_error, retry_delay


class HttpError(Exception):
    def __init__(self, status_code: int, text: str):
        self.status_code = status_code
        super().__init__(text)


class MockCompletions:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = 0

    def create(self, **kwargs):
        self.calls += 1
        result = self.outcomes.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


def _llm(monkeypatch, outcomes):
    completions = MockCompletions(outcomes)
    monkeypatch.setattr(HelloAgentsLLM, "_create_client", lambda _: SimpleNamespace(
        chat=SimpleNamespace(completions=completions),
    ))
    llm = HelloAgentsLLM(
        model="fake", provider="openai",
        api_key="test-secret", base_url="https://example.com/v1",
    )
    llm.max_retries = 2
    llm.retry_backoff = 0
    return llm, completions


def test_policy_distinguishes_retryable_and_terminal_errors():
    assert classify_model_error(HttpError(429, "too many requests")).kind == "rate_limited"
    assert classify_model_error(HttpError(503, "down")).retryable is True
    assert classify_model_error(TimeoutError("timed out")).retryable is True
    assert classify_model_error(HttpError(401, "bad token")).retryable is False
    assert classify_model_error(HttpError(400, "context_length_exceeded")).kind == "context_overflow"
    assert classify_model_error(HttpError(400, "bad tool param")).retryable is False
    assert classify_model_error(ValueError("unknown bug")).retryable is False
    assert retry_delay(999, 4.0) <= 8.0


def test_nonstream_transient_retries_then_succeeds(monkeypatch):
    llm, completions = _llm(monkeypatch, [
        HttpError(429, "rate limit"), HttpError(503, "temporary"), {"choices": []},
    ])
    assert llm.invoke_raw([{"role": "user", "content": "ping"}]) == {"choices": []}
    assert completions.calls == 3


def test_nonstream_auth_does_not_retry_or_leak_secret(monkeypatch):
    llm, completions = _llm(monkeypatch, [HttpError(401, "test-secret invalid")])
    with pytest.raises(ModelInvocationError) as ei:
        llm.invoke_raw([{"role": "user", "content": "ping"}])
    assert ei.value.kind == "unauthorized"
    assert "test-secret" not in str(ei.value)
    assert completions.calls == 1


def test_stream_before_first_chunk_may_retry(monkeypatch):
    chunk = SimpleNamespace(choices=[SimpleNamespace(
        delta=SimpleNamespace(content="ok"), finish_reason="stop")], usage=None)
    llm, completions = _llm(monkeypatch, [
        HttpError(503, "temporary"), iter([chunk]),
    ])
    events = []
    llm.stream_raw([{"role": "user", "content": "ping"}], on_delta=lambda kind, text: events.append(text))
    assert completions.calls == 2
    assert events == ["ok"]


def test_stream_after_first_chunk_must_never_retry(monkeypatch):
    chunk = SimpleNamespace(choices=[SimpleNamespace(
        delta=SimpleNamespace(content="partial"), finish_reason=None)], usage=None)

    def broken_stream():
        yield chunk
        raise ConnectionError("test-secret disconnected")

    llm, completions = _llm(monkeypatch, [broken_stream()])
    events = []
    with pytest.raises(ModelInvocationError) as ei:
        llm.stream_raw([{"role": "user", "content": "ping"}], on_delta=lambda kind, text: events.append(text))
    assert completions.calls == 1
    assert events == ["partial"]
    assert ei.value.kind == "stream_interrupted"
    assert "test-secret" not in str(ei.value)


def test_stream_context_overflow_does_not_retry(monkeypatch):
    llm, completions = _llm(monkeypatch, [HttpError(400, "context_length_exceeded")])
    with pytest.raises(ModelInvocationError) as ei:
        llm.stream_raw([{"role": "user", "content": "long"}])
    assert ei.value.kind == "context_overflow"
    assert completions.calls == 1
