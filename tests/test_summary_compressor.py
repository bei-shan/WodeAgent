"""SummaryCompressor tests."""

import concurrent.futures
import threading
import time
import unittest
from unittest.mock import patch
from types import SimpleNamespace

from core.context_engine.summary_compressor import (
    create_summary_generator,
    _serialize_messages_for_summary,
    _build_summary_prompt,
)
from core.message import Message
from core.config import Config


class DummyLLM:
    def __init__(self, response="Summary"):
        self.response = response

    def invoke(self, messages):
        return self.response


class FailingLLM:
    def invoke(self, messages):
        raise RuntimeError("fail")


class DummyFuture:
    def __init__(self):
        self.cancelled = False

    def result(self, timeout=None):
        raise concurrent.futures.TimeoutError()

    def cancel(self):
        # Model the standard concurrent.futures.Future.cancel() contract:
        # True means the task never started, so the caller must free capacity.
        self.cancelled = True
        return True


class DummyExecutor:
    def __init__(self, *args, **kwargs):
        self.calls = []
        self.future = DummyFuture()

    def submit(self, fn):
        return self.future

    def shutdown(self, wait=False, cancel_futures=False):
        self.calls.append({"wait": wait, "cancel_futures": cancel_futures})


class TestSummaryCompressor(unittest.TestCase):
    def test_create_generator(self):
        gen = create_summary_generator(DummyLLM())
        self.assertTrue(callable(gen))

    def test_generate_summary_empty_messages(self):
        gen = create_summary_generator(DummyLLM())
        self.assertIsNone(gen([]))

    def test_generate_summary_success(self):
        gen = create_summary_generator(DummyLLM(" Summary content "))
        msg = Message(content="hi", role="user")
        result = gen([msg])
        self.assertEqual(result, "Summary content")

    def test_generate_summary_llm_failure(self):
        gen = create_summary_generator(FailingLLM())
        msg = Message(content="hi", role="user")
        self.assertIsNone(gen([msg]))

    def test_timeout_handling(self):
        config = Config(summary_timeout=1)
        gen = create_summary_generator(DummyLLM(), config=config)
        msg = Message(content="hi", role="user")
        with patch("core.context_engine.summary_compressor.concurrent.futures.ThreadPoolExecutor", DummyExecutor):
            result = gen([msg])
        self.assertIsNone(result)

    def test_timeout_cancels_and_shutdown(self):
        config = Config(summary_timeout=1)
        gen = create_summary_generator(DummyLLM(), config=config)
        msg = Message(content="hi", role="user")
        executor = DummyExecutor()
        with patch("core.context_engine.summary_compressor.concurrent.futures.ThreadPoolExecutor", return_value=executor):
            result = gen([msg])
        self.assertIsNone(result)
        self.assertTrue(executor.future.cancelled)
        self.assertGreaterEqual(len(executor.calls), 1)
        self.assertTrue(any(call.get("cancel_futures") for call in executor.calls))

    def test_timeout_does_not_spawn_a_second_worker_until_first_exits(self):
        """Timeout does not cancel a running provider or free its capacity."""
        class BlockingLLM:
            def __init__(self):
                self.entered = threading.Event()
                self.release = threading.Event()
                self.calls = 0

            def invoke(self, messages):
                self.calls += 1
                self.entered.set()
                self.release.wait(timeout=3)
                return "completed"

        cfg = SimpleNamespace(
            summary_timeout=0.05,
            summary_input_max_chars=24000,
            summary_output_max_chars=10000,
        )
        llm = BlockingLLM()
        generator = create_summary_generator(llm, config=cfg)
        message = [Message(content="summarize", role="user")]
        try:
            self.assertIsNone(generator(message))
            self.assertTrue(llm.entered.is_set())
            start = time.monotonic()
            self.assertIsNone(generator(message))
            self.assertLess(time.monotonic() - start, 0.5)
            self.assertEqual(llm.calls, 1)
        finally:
            llm.release.set()
        # After the detached model call finally exits, a later attempt works.
        for _ in range(100):
            if generator(message) == "completed":
                break
            time.sleep(0.01)
        else:
            self.fail("single-flight permit was not released")

    def test_global_capacity_limits_multiple_generators(self):
        """Two sessions cannot both start when the process cap is exhausted."""
        class BlockingLLM:
            def __init__(self):
                self.entered = threading.Event()
                self.release = threading.Event()
                self.calls = 0

            def invoke(self, messages):
                self.calls += 1
                self.entered.set()
                self.release.wait(timeout=3)
                return "ok"

        cfg = SimpleNamespace(
            summary_timeout=0.05,
            summary_input_max_chars=24000,
            summary_output_max_chars=10000,
        )
        first_llm, second_llm = BlockingLLM(), BlockingLLM()
        messages = [Message(content="summarize", role="user")]
        # Patch a local one-slot cap, and keep it installed until the running
        # worker has actually released it. Avoid global cross-test leakage.
        slots = threading.BoundedSemaphore(1)
        with patch("core.context_engine.summary_compressor._SUMMARY_CALL_SLOTS", slots):
            first = create_summary_generator(first_llm, config=cfg)
            second = create_summary_generator(second_llm, config=cfg)
            try:
                self.assertIsNone(first(messages))
                self.assertTrue(first_llm.entered.is_set())
                start = time.monotonic()
                self.assertIsNone(second(messages))
                self.assertLess(time.monotonic() - start, 0.5)
                self.assertEqual(second_llm.calls, 0)
            finally:
                first_llm.release.set()
            for _ in range(100):
                if slots.acquire(blocking=False):
                    slots.release()
                    break
                time.sleep(0.01)
            else:
                self.fail("global summary capacity was not released")
            second_llm.release.set()
            self.assertEqual(second(messages), "ok")
            self.assertEqual(second_llm.calls, 1)

    def test_executor_submit_error_releases_capacity(self):
        """Executor startup failure must not permanently exhaust the pool."""
        class BrokenExecutor:
            def __init__(self, *args, **kwargs):
                pass

            def submit(self, callback):
                raise RuntimeError("failed before scheduling")

            def shutdown(self, wait=False, cancel_futures=False):
                pass

        messages = [Message(content="summarize", role="user")]
        generator = create_summary_generator(DummyLLM())
        with patch("core.context_engine.summary_compressor.concurrent.futures.ThreadPoolExecutor", BrokenExecutor):
            self.assertIsNone(generator(messages))
            self.assertIsNone(generator(messages))

    def test_serialize_messages_for_summary(self):
        long_text = "x" * 600
        messages = [
            Message(content="hello", role="user"),
            Message(content="hi", role="assistant"),
            Message(content=long_text, role="tool", metadata={"tool_name": "LS"}),
            Message(content="prev", role="summary"),
        ]
        text = _serialize_messages_for_summary(messages)
        self.assertIn("[User]: hello", text)
        self.assertIn("[Assistant]: hi", text)
        self.assertIn("[Tool:LS]:", text)
        self.assertIn("[Previous Summary]: prev", text)
        self.assertIn("...", text)

    def test_build_summary_prompt_contains_conversation(self):
        conversation = "[User]: hi"
        prompt = _build_summary_prompt(conversation)
        self.assertIn(conversation, prompt)
        self.assertIn("Here is the conversation history", prompt)

    def test_build_summary_prompt_fallback_on_import_error(self):
        conversation = "[User]: hi"
        with patch("builtins.__import__", side_effect=ImportError("missing")):
            prompt = _build_summary_prompt(conversation)
        self.assertIn("ARCHIVED SESSION SUMMARY", prompt)
        self.assertIn(conversation, prompt)


if __name__ == "__main__":
    unittest.main()
