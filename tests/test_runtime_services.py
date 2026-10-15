"""RuntimeServices owns state independently of optional Feature hooks."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from core.runtime.services import RuntimeServices


class Service:
    def __init__(self, name, calls, *, fail=False, fail_close=False):
        self.name = name
        self.calls = calls
        self.fail = fail
        self.fail_close = fail_close

    def start(self, agent):
        self.calls.append((self.name, "start"))
        if self.fail:
            raise RuntimeError("partially started")

    def close(self, agent):
        self.calls.append((self.name, "close"))
        if self.fail_close:
            raise RuntimeError("failed stop")


def test_dependency_first_start_and_reverse_shutdown():
    calls = []
    services = RuntimeServices(SimpleNamespace())
    services.register("core", lambda: Service("core", calls))
    services.register("mcp", lambda: Service("mcp", calls), requires=("core",))
    services.register("team", lambda: Service("team", calls), requires=("mcp",))
    assert services.ensure("team") is services.get("team")
    assert services.ensure("team") is services.get("team")
    assert services.started == ("core", "mcp", "team")
    services.close()
    services.close()
    assert calls == [
        ("core", "start"), ("mcp", "start"), ("team", "start"),
        ("team", "close"), ("mcp", "close"), ("core", "close"),
    ]
    with pytest.raises(RuntimeError, match="closed"):
        services.ensure("team")


def test_cycles_unknown_deps_and_duplicate_register_fail_closed():
    services = RuntimeServices(object())
    services.register("a", lambda: Service("a", []), requires=("b",))
    services.register("b", lambda: Service("b", []), requires=("a",))
    with pytest.raises(RuntimeError, match="Cyclic"):
        services.ensure("a")
    with pytest.raises(ValueError, match="duplicate"):
        services.register("a", lambda: Service("no", []))
    other = RuntimeServices(object())
    other.register("app", lambda: Service("app", []), requires=("missing",))
    with pytest.raises(LookupError, match="missing"):
        other.ensure("app")


def test_failed_start_is_disposed_and_does_not_start_following_service():
    calls = []
    services = RuntimeServices(object())
    services.register("first", lambda: Service("first", calls))
    services.register("bad", lambda: Service("bad", calls, fail=True),
                      requires=("first",))
    services.register("never", lambda: Service("never", calls))
    with pytest.raises(RuntimeError, match="partially started"):
        services.ensure("bad")
    services.close()
    assert calls == [
        ("first", "start"), ("bad", "start"),
        ("bad", "close"), ("first", "close"),
    ]


def test_failed_close_does_not_skip_other_resources(caplog):
    calls = []
    services = RuntimeServices(object())
    services.register("first", lambda: Service("first", calls))
    services.register("last", lambda: Service("last", calls, fail_close=True))
    services.ensure("first")
    services.ensure("last")
    services.close()
    assert calls[-2:] == [("last", "close"), ("first", "close")]
    assert "last cleanup failed" in caplog.text


def test_registration_requires_valid_resource_contract():
    graph = RuntimeServices(object())
    with pytest.raises(ValueError):
        graph.register("self", lambda: object(), requires=("self",))
    graph.register("broken", lambda: object())
    with pytest.raises(TypeError, match="Invalid runtime service"):
        graph.ensure("broken")
    graph.close()
