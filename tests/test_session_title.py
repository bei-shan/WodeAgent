"""Session title generation: placeholder detection, cleaning, bounded calls."""
from __future__ import annotations

import time

from core.session_title import (
    build_title_prompt,
    clean_title,
    generate_title,
    is_placeholder_title,
)


class _FakeLLM:
    def __init__(self, reply):
        self.reply = reply
        self.calls = []

    def invoke(self, messages):
        self.calls.append(messages)
        if isinstance(self.reply, Exception):
            raise self.reply
        if callable(self.reply):
            return self.reply()
        return self.reply


def test_placeholder_detection():
    assert is_placeholder_title("")
    assert is_placeholder_title(None)
    assert is_placeholder_title("New session 2026-10-15T23:24")
    assert is_placeholder_title("新会话")
    assert is_placeholder_title("017d2a1e")
    assert not is_placeholder_title("Fix the login redirect loop")


def test_clean_title_strips_decoration_and_truncates():
    assert clean_title('"Fix the login bug".') == "Fix the login bug"
    assert clean_title("**修复登录跳转**") == "修复登录跳转"
    assert clean_title("Title: Add retry to uploader") == "Add retry to uploader"
    assert clean_title("   ") is None
    assert clean_title(None) is None
    long_title = clean_title("word " * 40)
    assert long_title is not None and len(long_title) <= 40


def test_prompt_is_bounded_and_keeps_first_exchange():
    messages = [
        {"role": "system", "content": "ignored"},
        {"role": "user", "content": "u" * 5000},
        {"role": "assistant", "content": "a" * 5000},
    ]
    prompt = build_title_prompt(messages)
    assert "user:" in prompt and "assistant:" in prompt
    assert "system:" not in prompt
    assert len(prompt) < 3000
    assert "<conversation>" in prompt


def test_generate_title_accepts_clean_reply():
    llm = _FakeLLM("Add retry to uploader")
    title = generate_title(llm, [{"role": "user", "content": "make uploads retry"}])
    assert title == "Add retry to uploader"
    assert llm.calls and llm.calls[0][0]["role"] == "user"


def test_generate_title_returns_none_on_error():
    llm = _FakeLLM(RuntimeError("provider down"))
    assert generate_title(llm, [{"role": "user", "content": "hi"}]) is None


def test_generate_title_times_out_without_raising():
    def slow():
        time.sleep(2)
        return "too late"

    llm = _FakeLLM(slow)
    started = time.monotonic()
    assert generate_title(llm, [{"role": "user", "content": "hi"}], timeout_s=0.05) is None
    assert time.monotonic() - started < 1.5
