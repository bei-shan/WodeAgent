"""Registry guards: read-before-write and cooperative tool timeouts."""
from __future__ import annotations

import json
import time

from tools.base import Tool
from tools.registry import ToolRegistry


class _SlowTool(Tool):
    name = "Slow"
    description = "Sleeps past its budget."
    timeout_seconds = 0.05

    def __init__(self):
        super().__init__(name=self.name, description=self.description)

    def get_parameters(self):
        return []

    def run(self, parameters):
        time.sleep(0.6)
        return self.create_success_response(
            data={"late": True}, text="late", params_input=parameters, time_ms=1,
        )


class _WriteTool(Tool):
    name = "Write"
    description = "Writes without touching disk."

    def __init__(self):
        super().__init__(name=self.name, description=self.description)

    def get_parameters(self):
        return []

    def run(self, parameters):
        return self.create_success_response(
            data={}, text="wrote", params_input=parameters, time_ms=1,
        )


def _registry() -> ToolRegistry:
    registry = ToolRegistry()
    registry.register_tool(_SlowTool())
    registry.register_tool(_WriteTool())
    return registry


def _observe(registry: ToolRegistry, path: str) -> None:
    """Pretend the agent read *path* (what Read would populate)."""
    registry._read_cache[path] = {
        "path_resolved": path,
        "file_mtime_ms": 1,
        "file_size_bytes": 1,
        "captured_at": time.time(),
    }


def test_slow_tool_returns_timeout_instead_of_hanging():
    registry = _registry()
    started = time.monotonic()
    payload = json.loads(registry.execute_tool("Slow", {}))
    elapsed = time.monotonic() - started
    assert payload["status"] == "error"
    assert payload["error"]["code"] == "TIMEOUT"
    assert elapsed < 0.5  # returned at the budget, not after the sleep


def test_overwriting_unread_file_is_refused(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "existing.txt").write_text("hello", encoding="utf-8")
    registry = _registry()

    payload = json.loads(registry.execute_tool("Write", {"path": "existing.txt"}))
    assert payload["status"] == "error"
    assert payload["error"]["code"] == "ACCESS_DENIED"
    assert "Read" in payload["error"]["message"]


def test_overwriting_after_read_is_allowed(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "existing.txt").write_text("hello", encoding="utf-8")
    registry = _registry()
    _observe(registry, "existing.txt")

    payload = json.loads(registry.execute_tool("Write", {"path": "existing.txt"}))
    assert payload["status"] == "success"


def test_creating_a_new_file_needs_no_prior_read(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    registry = _registry()
    payload = json.loads(registry.execute_tool("Write", {"path": "brand-new.txt"}))
    assert payload["status"] == "success"


def test_explicit_expectations_bypass_the_policy(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "existing.txt").write_text("hello", encoding="utf-8")
    registry = _registry()
    payload = json.loads(registry.execute_tool(
        "Write",
        {"path": "existing.txt", "expected_mtime_ms": 1, "expected_size_bytes": 1},
    ))
    assert payload["status"] == "success"


def test_policy_can_be_disabled(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("AGENT_REQUIRE_READ_BEFORE_WRITE", "false")
    (tmp_path / "existing.txt").write_text("hello", encoding="utf-8")
    registry = _registry()
    payload = json.loads(registry.execute_tool("Write", {"path": "existing.txt"}))
    assert payload["status"] == "success"


def test_read_cache_still_injects_optimistic_lock_values(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "existing.txt").write_text("hello", encoding="utf-8")
    registry = _registry()
    registry._read_cache["existing.txt"] = {
        "path_resolved": "existing.txt",
        "file_mtime_ms": 1700000000000,
        "file_size_bytes": 5,
        "captured_at": time.time(),
    }
    injected = registry._inject_optimistic_lock_params("Write", {"path": "existing.txt"})
    assert injected["expected_mtime_ms"] == 1700000000000
    assert injected["expected_size_bytes"] == 5
