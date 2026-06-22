"""VCR (Video Cassette Recorder) — LLM API call recording and replay.

Records real LLM request/response pairs as JSON fixtures under
``tests/fixtures/vcr/``.  During test runs, replays cached responses
instead of calling the real API — making tests deterministic, free,
and fast.

Usage::

    vcr = VCR(fixture_dir="tests/fixtures/vcr")
    response = vcr.call(
        model="deepseek-v4-pro",
        messages=[...],
        tools=[...],
        fallback=lambda: llm.invoke_raw(messages, tools=[...]),
    )

Configuration via env vars::

    VCR_ENABLED=true            # enable VCR interception
    VCR_RECORD_MODE=new_episodes  # new_episodes | once | none
    VCR_FIXTURE_DIR=tests/fixtures/vcr
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)

_DEFAULT_FIXTURE_DIR = "tests/fixtures/vcr"


@dataclass
class _VCRInput:
    """Normalised representation of an LLM call for fingerprinting."""

    model: str
    messages: list[dict[str, Any]]
    tools: Optional[list[dict[str, Any]]]
    tool_choice: str

    def fingerprint(self, cwd: str) -> str:
        """Return a stable hex fingerprint for this input."""
        dehydrated_messages = [_dehydrate_message(m, cwd) for m in self.messages]
        dehydrated_tools = self.tools or []
        payload = json.dumps(
            {
                "model": self.model,
                "messages": dehydrated_messages,
                "tools": dehydrated_tools,
                "tool_choice": self.tool_choice,
            },
            sort_keys=True,
            ensure_ascii=True,
        )
        return hashlib.sha256(payload.encode()).hexdigest()[:16]


class VCR:
    """LLM API call recorder / replayer.

    Parameters
    ----------
    fixture_dir:
        Directory where fixture JSON files are stored.
    enabled:
        When ``False`` (default for non-test), VCR is a transparent pass-through.
    record_mode:
        ``"new_episodes"`` — replay existing, record new.
        ``"once"`` — replay existing, record new (same behaviour in v1).
        ``"none"`` — replay existing, raise on missing.
    """

    def __init__(
        self,
        fixture_dir: str = _DEFAULT_FIXTURE_DIR,
        enabled: bool = False,
        record_mode: str = "new_episodes",
    ):
        self._fixture_dir = Path(fixture_dir)
        self._enabled = enabled
        self._record_mode = record_mode
        self._cwd = os.getcwd()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @property
    def enabled(self) -> bool:
        return self._enabled

    def call(
        self,
        *,
        model: str,
        messages: list[dict[str, Any]],
        tools: Optional[list[dict[str, Any]]] = None,
        tool_choice: str = "auto",
        fallback: Callable[[], Any],
    ) -> Any:
        """VCR-wrapped LLM call.

        If VCR is disabled, calls *fallback* directly.
        Otherwise checks for a cached fixture; replays if found,
        records if missing (depending on *record_mode*).

        Parameters
        ----------
        model:
            The model identifier (used for fingerprinting).
        messages:
            The full message list sent to the LLM.
        tools:
            OpenAI-format tool schemas.
        tool_choice:
            Tool choice mode (``"auto"``, ``"none"``, etc.).
        fallback:
            Zero-argument callable that performs the real API call.

        Returns
        -------
        The raw LLM response object (or reconstructed mock).
        """
        if not self._enabled:
            return fallback()

        inp = _VCRInput(
            model=model,
            messages=messages,
            tools=tools,
            tool_choice=tool_choice,
        )
        fp = inp.fingerprint(self._cwd)
        fixture_path = self._fixture_dir / f"{fp}.json"

        # Replay
        if fixture_path.exists():
            logger.debug("VCR replay: %s", fixture_path.name)
            return self._replay(fixture_path)

        # Missing fixture
        if self._record_mode == "none":
            raise VCRFixtureMissing(
                f"VCR fixture missing: {fixture_path}\n"
                f"Run with VCR_RECORD_MODE=new_episodes to generate it."
            )

        # Record
        logger.info("VCR record: %s", fixture_path.name)
        result = fallback()

        if self._record_mode in ("new_episodes", "once"):
            self._record(fixture_path, inp, result)

        return result

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _replay(self, path: Path) -> Any:
        """Reconstruct a mock response from a fixture file."""
        data = json.loads(path.read_text(encoding="utf-8"))
        return _hydrate_response(data["output"])

    def _record(self, path: Path, inp: _VCRInput, result: Any) -> None:
        """Persist a fixture file."""
        path.parent.mkdir(parents=True, exist_ok=True)

        serialisable = _make_serialisable(result)
        fixture = {
            "version": 1,
            "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "input": {
                "model": inp.model,
                "messages_hash": inp.fingerprint(self._cwd),
                "messages_summary": _summarise_messages(inp.messages),
                "tools_count": len(inp.tools) if inp.tools else 0,
                "tool_choice": inp.tool_choice,
            },
            "output": serialisable,
        }
        tmp_path = path.with_suffix(".tmp")
        tmp_path.write_text(
            json.dumps(fixture, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        tmp_path.replace(path)  # atomic rename

    @classmethod
    def from_env(cls) -> "VCR":
        """Create a VCR instance from environment variables.

        Reads ``VCR_ENABLED``, ``VCR_RECORD_MODE``, ``VCR_FIXTURE_DIR``.
        """
        enabled = os.getenv("VCR_ENABLED", "").lower() in (
            "1", "true", "yes", "y", "on",
        )
        record_mode = os.getenv("VCR_RECORD_MODE", "new_episodes").lower()
        fixture_dir = os.getenv("VCR_FIXTURE_DIR", _DEFAULT_FIXTURE_DIR)
        return cls(
            fixture_dir=fixture_dir,
            enabled=enabled,
            record_mode=record_mode,
        )


class VCRFixtureMissing(Exception):
    """Raised when a VCR fixture is needed but not found in ``none`` mode."""
    pass


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _dehydrate_message(msg: dict[str, Any], cwd: str) -> dict[str, Any]:
    """Strip non-deterministic content from a single message."""
    dehydrated = dict(msg)
    content = dehydrated.get("content")
    if content is None:
        return dehydrated

    text = str(content)

    # Replace CWD
    if cwd:
        text = text.replace(cwd, "[CWD]")
        # Windows backslash variant
        text = text.replace(cwd.replace("/", "\\"), "[CWD]")

    # Replace common temp paths
    text = re.sub(
        r"(?:/tmp|C:\\Users\\[^\\]+\\AppData\\Local\\Temp)[/\\][a-zA-Z0-9_.\-/\\]+",
        "[TMP]",
        text,
    )

    # Replace timestamps
    text = re.sub(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})?", "[TS]", text)

    # Replace UUIDs
    text = re.sub(
        r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}",
        "[UUID]",
        text,
    )

    dehydrated["content"] = text
    return dehydrated


def _summarise_messages(messages: list[dict[str, Any]]) -> str:
    """Return a human-readable summary of a message list for fixture metadata."""
    roles: dict[str, int] = {}
    for m in messages:
        role = str(m.get("role", "?"))
        roles[role] = roles.get(role, 0) + 1
    parts = [f"{r}({c})" for r, c in sorted(roles.items())]
    return ", ".join(parts)


def _make_serialisable(result: Any) -> dict[str, Any]:
    """Convert an OpenAI response object to a plain dict for JSON storage."""
    if hasattr(result, "model_dump"):
        return result.model_dump(mode="json")
    if hasattr(result, "to_dict"):
        return result.to_dict()
    if isinstance(result, dict):
        return result
    return {"_raw": str(result)}


def _hydrate_response(data: dict[str, Any]) -> Any:
    """Reconstruct a mock response object from a fixture dict.

    Returns a lightweight object that mimics the OpenAI response interface
    well enough for ``extract_content`` / ``extract_tool_calls`` / etc.
    """
    return _MockResponse(data)


class _MockResponse:
    """Minimal mock of an OpenAI chat completion response."""

    def __init__(self, data: dict[str, Any]):
        self._data = data

    def model_dump(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        return self._data

    def __getattr__(self, name: str) -> Any:
        if name == "choices":
            raw_choices = self._data.get("choices", [])
            return [_MockChoice(c) for c in raw_choices]
        if name == "usage":
            return self._data.get("usage", {})
        if name == "id":
            return self._data.get("id", "vcr-mock")
        if name == "model":
            return self._data.get("model", "")
        if name == "object":
            return self._data.get("object", "chat.completion")
        if name in self._data:
            return self._data[name]
        raise AttributeError(name)


class _MockChoice:
    """Minimal mock of an OpenAI chat completion choice."""

    def __init__(self, data: dict[str, Any]):
        self._data = data

    def __getattr__(self, name: str) -> Any:
        if name == "message":
            return _MockMessage(self._data.get("message", {}))
        if name == "finish_reason":
            return self._data.get("finish_reason", "stop")
        if name == "index":
            return self._data.get("index", 0)
        if name in self._data:
            return self._data[name]
        raise AttributeError(name)


class _MockMessage:
    """Minimal mock of an OpenAI chat completion message."""

    def __init__(self, data: dict[str, Any]):
        self._data = data
        self.content = data.get("content")
        self.role = data.get("role", "assistant")
        self.tool_calls = data.get("tool_calls")
        self.function_call = data.get("function_call")

    def __getattr__(self, name: str) -> Any:
        if name in self._data:
            return self._data[name]
        raise AttributeError(name)
