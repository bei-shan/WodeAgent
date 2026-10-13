"""Trace sanitization must scrub credentials in unstructured outputs."""
from core.context_engine.trace_sanitizer import TraceSanitizer


def test_bearer_token_redacted_in_text_output():
    token = "abcdefghijklmnopqrstuvwxyz0123456789"
    value = TraceSanitizer().sanitize({"text": "Authorization: Bearer " + token})
    assert token not in value["text"]
    assert "Bearer ***" in value["text"]
