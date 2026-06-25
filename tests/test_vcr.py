"""Tests for VCR (LLM API recording/replay)."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from core.vcr import (
    VCR,
    VCRFixtureMissing,
    _dehydrate_message,
    _MockResponse,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_mock_response(content: str = "Hello", tool_calls=None) -> MagicMock:
    """Build a mock OpenAI response object."""
    resp = MagicMock()
    resp.id = "chatcmpl-test-001"
    resp.model = "deepseek-v4-pro"
    resp.object = "chat.completion"

    choice = MagicMock()
    choice.index = 0
    choice.finish_reason = "stop"

    msg = MagicMock()
    msg.role = "assistant"
    msg.content = content
    msg.tool_calls = tool_calls
    choice.message = msg

    resp.choices = [choice]
    resp.usage = {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}

    # Make model_dump() return a plain dict (not another MagicMock).
    def _model_dump(mode=None):
        return {
            "id": "chatcmpl-test-001",
            "model": "deepseek-v4-pro",
            "object": "chat.completion",
            "choices": [{
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": content,
                    "tool_calls": tool_calls,
                },
                "finish_reason": "stop",
            }],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        }
    resp.model_dump = _model_dump

    return resp


def _make_messages() -> list[dict]:
    return [
        {"role": "system", "content": "You are a helpful assistant."},
        {"role": "user", "content": "Hello!"},
    ]


# ---------------------------------------------------------------------------
# Core tests
# ---------------------------------------------------------------------------

class TestVCRDisabled:
    """When VCR is disabled, it should pass through to fallback."""

    def test_disabled_calls_fallback(self):
        vcr = VCR(enabled=False)
        called = []

        def fallback():
            called.append(True)
            return _make_mock_response("fallback")

        result = vcr.call(
            model="test-model",
            messages=_make_messages(),
            fallback=fallback,
        )
        assert called == [True]
        assert result.choices[0].message.content == "fallback"

    def test_disabled_never_writes_fixture(self, tmp_path):
        fixture_dir = tmp_path / "fixtures"
        vcr = VCR(fixture_dir=str(fixture_dir), enabled=False)

        vcr.call(
            model="test-model",
            messages=_make_messages(),
            fallback=lambda: _make_mock_response("ok"),
        )
        assert not list(fixture_dir.glob("*.json"))


class TestVCRRecordAndReplay:
    """Test recording new fixtures and replaying them."""

    def test_records_and_replays(self, tmp_path):
        fixture_dir = tmp_path / "fixtures"
        vcr = VCR(fixture_dir=str(fixture_dir), enabled=True, record_mode="new_episodes")
        messages = _make_messages()

        # First call: should record.
        call_count = [0]

        def fallback():
            call_count[0] += 1
            return _make_mock_response("recorded response")

        result1 = vcr.call(
            model="test-model",
            messages=messages,
            fallback=fallback,
        )
        assert result1.choices[0].message.content == "recorded response"
        assert call_count[0] == 1

        # Verify fixture was written.
        fixtures = list(fixture_dir.glob("*.json"))
        assert len(fixtures) == 1

        # Second call: should replay from fixture.
        result2 = vcr.call(
            model="test-model",
            messages=messages,
            fallback=lambda: _make_mock_response("SHOULD NOT BE CALLED"),
        )
        assert result2.choices[0].message.content == "recorded response"
        assert call_count[0] == 1  # fallback not called again

    def test_different_messages_produce_different_fixtures(self, tmp_path):
        fixture_dir = tmp_path / "fixtures"
        vcr = VCR(fixture_dir=str(fixture_dir), enabled=True)

        vcr.call(
            model="test",
            messages=[{"role": "user", "content": "msg A"}],
            fallback=lambda: _make_mock_response("A"),
        )
        vcr.call(
            model="test",
            messages=[{"role": "user", "content": "msg B"}],
            fallback=lambda: _make_mock_response("B"),
        )

        fixtures = list(fixture_dir.glob("*.json"))
        assert len(fixtures) == 2

    def test_different_models_produce_different_fixtures(self, tmp_path):
        fixture_dir = tmp_path / "fixtures"
        vcr = VCR(fixture_dir=str(fixture_dir), enabled=True)
        msgs = _make_messages()

        vcr.call(model="model-a", messages=msgs, fallback=lambda: _make_mock_response("A"))
        vcr.call(model="model-b", messages=msgs, fallback=lambda: _make_mock_response("B"))

        fixtures = list(fixture_dir.glob("*.json"))
        assert len(fixtures) == 2


class TestVCRRecordModes:
    """Test different record mode behaviors."""

    def test_none_mode_raises_on_missing(self, tmp_path):
        fixture_dir = tmp_path / "fixtures"
        vcr = VCR(fixture_dir=str(fixture_dir), enabled=True, record_mode="none")

        with pytest.raises(VCRFixtureMissing):
            vcr.call(
                model="test",
                messages=_make_messages(),
                fallback=lambda: _make_mock_response("x"),
            )

    def test_once_mode_calls_through_without_persisting(self, tmp_path):
        """once mode calls real API but doesn't persist fixtures."""
        fixture_dir = tmp_path / "fixtures"
        vcr = VCR(fixture_dir=str(fixture_dir), enabled=True, record_mode="once")

        result = vcr.call(
            model="test",
            messages=_make_messages(),
            fallback=lambda: _make_mock_response("once-through"),
        )
        assert result.choices[0].message.content == "once-through"
        # once mode does NOT record — no fixture file created
        assert len(list(fixture_dir.glob("*.json"))) == 0


class TestDehydrate:
    """Test message dehydration for stable fingerprints."""

    def test_replaces_cwd(self):
        msg = {"role": "user", "content": "Read file /home/user/project/src/main.py"}
        result = _dehydrate_message(msg, "/home/user/project")
        assert "[CWD]" in result["content"]
        assert "/home/user/project" not in result["content"]

    def test_replaces_windows_cwd(self):
        msg = {"role": "user", "content": "Read file C:\\Users\\test\\project\\src\\main.py"}
        result = _dehydrate_message(msg, "C:/Users/test/project")
        # Should replace backslash variant
        assert "C:\\Users\\test\\project" not in result["content"]

    def test_replaces_temp_paths(self):
        msg = {"role": "user", "content": "Read /tmp/abc123/file.txt"}
        result = _dehydrate_message(msg, "/other")
        assert "[TMP]" in result["content"]

    def test_replaces_uuids(self):
        msg = {"role": "user", "content": "id: 550e8400-e29b-41d4-a716-446655440000"}
        result = _dehydrate_message(msg, "/tmp")
        assert "[UUID]" in result["content"]

    def test_replaces_timestamps(self):
        msg = {"role": "user", "content": "at 2026-06-22T10:30:00Z event happened"}
        result = _dehydrate_message(msg, "/tmp")
        assert "[TS]" in result["content"]

    def test_none_content_unchanged(self):
        msg = {"role": "system", "content": None}
        result = _dehydrate_message(msg, "/tmp")
        assert result["content"] is None


class TestMockResponse:
    """Test the mock response objects used for replay."""

    def test_mock_response_model_dump(self):
        data = {
            "id": "test-id",
            "model": "test-model",
            "choices": [{
                "index": 0,
                "message": {"role": "assistant", "content": "Hi"},
                "finish_reason": "stop",
            }],
            "usage": {"total_tokens": 10},
        }
        mock = _MockResponse(data)
        assert mock.model_dump() == data
        assert mock.id == "test-id"
        assert mock.model == "test-model"
        assert len(mock.choices) == 1
        assert mock.choices[0].message.content == "Hi"
        assert mock.choices[0].finish_reason == "stop"

    def test_mock_response_with_tool_calls(self):
        data = {
            "choices": [{
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [{
                        "id": "call_1",
                        "type": "function",
                        "function": {"name": "Read", "arguments": '{"file_path":"/x"}'},
                    }],
                },
                "finish_reason": "tool_calls",
            }],
        }
        mock = _MockResponse(data)
        msg = mock.choices[0].message
        assert msg.tool_calls is not None
        assert msg.tool_calls[0]["function"]["name"] == "Read"


class TestFromEnv:
    """Test VCR.from_env() factory."""

    def test_defaults_disabled(self):
        with patch.dict(os.environ, {}, clear=True):
            vcr = VCR.from_env()
            assert vcr.enabled is False

    def test_enabled_true(self):
        with patch.dict(os.environ, {"VCR_ENABLED": "true"}, clear=True):
            vcr = VCR.from_env()
            assert vcr.enabled is True

    def test_enabled_yes(self):
        with patch.dict(os.environ, {"VCR_ENABLED": "yes"}, clear=True):
            vcr = VCR.from_env()
            assert vcr.enabled is True

    def test_custom_fixture_dir(self):
        with patch.dict(os.environ, {"VCR_FIXTURE_DIR": "my/fixtures"}, clear=True):
            vcr = VCR.from_env()
            assert vcr._fixture_dir == Path("my/fixtures")

    def test_record_mode(self):
        with patch.dict(os.environ, {"VCR_RECORD_MODE": "none"}, clear=True):
            vcr = VCR.from_env()
            assert vcr._record_mode == "none"


class TestFixtureFormat:
    """Test the JSON fixture format."""

    def test_fixture_has_version_and_timestamp(self, tmp_path):
        fixture_dir = tmp_path / "fixtures"
        vcr = VCR(fixture_dir=str(fixture_dir), enabled=True)

        vcr.call(
            model="test",
            messages=_make_messages(),
            fallback=lambda: _make_mock_response("hello"),
        )

        fixture = json.loads(list(fixture_dir.glob("*.json"))[0].read_text())
        assert fixture["version"] == 1
        assert "created_at" in fixture
        assert "input" in fixture
        assert "output" in fixture
        assert fixture["input"]["model"] == "test"
