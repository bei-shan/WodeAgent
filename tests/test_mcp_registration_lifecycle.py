"""Deterministic registration retirement and background publication races."""
from __future__ import annotations

import threading
import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from tools.mcp import loader
from tools.mcp.client import MCPClient, MCPClientConfig
from tools.registry import ToolRegistry


def fake_client(tool_name="read"):
    client = MagicMock()
    client.list_tools_sync.return_value = SimpleNamespace(tools=[SimpleNamespace(
        name=tool_name, description=tool_name,
        inputSchema={"type": "object", "properties": {}},
    )])
    return client


def configure(monkeypatch, clients, mode="startup"):
    monkeypatch.setattr(loader, "load_mcp_servers", lambda root: {"shared": {"command": "echo"}})
    monkeypatch.setattr(loader, "connect_mode", lambda: mode)
    monkeypatch.setattr(loader, "MCPClient", lambda config: next(clients))


def test_failed_startup_closes_client_without_retaining_a_registration(monkeypatch, tmp_path):
    failed = fake_client()
    failed.list_tools_sync.side_effect = RuntimeError("connected, but listing failed")
    replacement = fake_client()
    configure(monkeypatch, iter([failed, replacement]))
    registry = ToolRegistry()

    clients, meta = loader.register_mcp_servers(registry, str(tmp_path))

    assert failed.close_sync.call_count == 1
    assert clients == []
    assert meta == []
    assert registry not in loader._shared_users
    assert loader.release_mcp_registration(registry) is False
    try:
        clients, _ = loader.register_mcp_servers(registry, str(tmp_path))
        assert clients == [replacement]
    finally:
        loader.release_mcp_registration(registry)
    assert failed.close_sync.call_count == replacement.close_sync.call_count == 1


@pytest.mark.parametrize("first_fails", [False, True])
@pytest.mark.parametrize("bad_config", [False, True])
def test_later_server_setup_failure_preserves_lifecycle_ownership(
    monkeypatch, tmp_path, first_fails, bad_config,
):
    first = fake_client()
    if first_fails:
        first.list_tools_sync.side_effect = RuntimeError("listing failed")
    configure(monkeypatch, iter([first]))
    monkeypatch.setattr(loader, "load_mcp_servers", lambda root: {
        "shared": {"command": "echo"},
        "broken": {"transport": "http"} if bad_config else {"command": "echo"},
    })

    def build(config):
        if config.name == "broken":
            raise RuntimeError("client construction failed")
        return first

    monkeypatch.setattr(loader, "MCPClient", build)
    registry = ToolRegistry()
    try:
        clients, _ = loader.register_mcp_servers(registry, str(tmp_path))
        assert clients == ([] if first_fails else [first])
        assert (registry in loader._shared_users) is not first_fails
        assert first.close_sync.call_count == int(first_fails)
    finally:
        loader.release_mcp_registration(registry)
    assert first.close_sync.call_count == 1


def test_retired_teardown_cannot_remove_a_new_generation(monkeypatch, tmp_path):
    old, new = fake_client("old"), fake_client("new")
    configure(monkeypatch, iter([old, new]))
    registry = ToolRegistry()
    loader.register_mcp_servers(registry, str(tmp_path))
    unlocked, resume = threading.Event(), threading.Event()
    underlying = loader._shared_lock

    class PauseAfterUnlock:
        def __enter__(self):
            return underlying.__enter__()

        def __exit__(self, *args):
            result = underlying.__exit__(*args)
            if threading.current_thread().name == "release-old":
                unlocked.set()
                assert resume.wait(5)
            return result

    monkeypatch.setattr(loader, "_shared_lock", PauseAfterUnlock())
    release = threading.Thread(target=loader.release_mcp_registration, args=(registry,), name="release-old")
    release.start()
    try:
        assert unlocked.wait(5)
        loader.register_mcp_servers(registry, str(tmp_path))
    finally:
        resume.set()
        release.join(5)
    try:
        assert not release.is_alive()
        assert registry.get_tool("shared_new") is not None
        assert registry.get_tool("shared_new")._mcp_client is new
        assert old.close_sync.call_count == 1
        assert new.close_sync.call_count == 0
    finally:
        loader.release_mcp_registration(registry)


@pytest.mark.parametrize("outcome", ["success", "failure"])
@pytest.mark.parametrize("timeout_first", [False, True])
@pytest.mark.parametrize("blocked_operation", ["connect", "listing"])
def test_late_background_completion_cannot_publish_into_new_generation(
    monkeypatch, tmp_path, outcome, timeout_first, blocked_operation,
):
    old, new = fake_client("old"), fake_client("new")
    entered, resume, completed = threading.Event(), threading.Event(), threading.Event()

    def remote_operation():
        entered.set()
        assert resume.wait(5)
        if outcome == "failure":
            raise RuntimeError("old connection failed")
        return old.list_tools_sync.return_value

    operation = old.connect_sync if blocked_operation == "connect" else old.list_tools_sync
    operation.side_effect = remote_operation
    original_background = loader._background_connect

    def background(*args, **kwargs):
        try:
            return original_background(*args, **kwargs)
        finally:
            completed.set()

    configure(monkeypatch, iter([old, new]), mode="manual")
    monkeypatch.setattr(loader, "_background_connect", background)
    workers = []
    original_thread = threading.Thread

    def track_thread(*args, **kwargs):
        thread = original_thread(*args, **kwargs)
        workers.append(thread)
        return thread

    monkeypatch.setattr(loader.threading, "Thread", track_thread)
    if timeout_first:
        # The connect worker is event-blocked, so a zero deadline deterministically
        # records pending before release without relying on scheduler timing.
        monkeypatch.setattr(loader, "_DEFAULT_CONNECT_TIMEOUT", 0)
    registry = ToolRegistry()
    loader.register_mcp_servers(registry, str(tmp_path))
    try:
        assert entered.wait(5)
        if timeout_first:
            assert completed.wait(5)
            assert loader.get_pending_server_names(registry) == ["shared"]
        assert loader.release_mcp_registration(registry)
        monkeypatch.setattr(loader, "connect_mode", lambda: "startup")
        _, expected_meta = loader.register_mcp_servers(registry, str(tmp_path))
    finally:
        resume.set()
        assert completed.wait(5)
        for worker in workers:
            worker.join(5)
            assert not worker.is_alive()
    try:
        assert registry.get_tool("shared_old") is None
        assert registry.get_tool("shared_new")._mcp_client is new
        assert loader._shared_tools_meta[(registry, "shared")] == expected_meta
        assert loader.get_pending_server_names(registry) == []
        assert old.close_sync.call_count == 1
        expected_calls = 0 if blocked_operation == "connect" else 1
        assert old.list_tools_sync.call_count == expected_calls, "retired client started another remote operation"
    finally:
        loader.release_mcp_registration(registry)


def test_close_io_does_not_hold_global_registration_lock(monkeypatch, tmp_path):
    old, new = fake_client(), fake_client()
    configure(monkeypatch, iter([old, new]))
    first, second = ToolRegistry(), ToolRegistry()
    loader.register_mcp_servers(first, str(tmp_path))
    closing, resume, registered = threading.Event(), threading.Event(), threading.Event()

    def close():
        closing.set()
        assert resume.wait(5)

    old.close_sync.side_effect = close
    release = threading.Thread(target=loader.release_mcp_registration, args=(first,))
    release.start()

    def register():
        loader.register_mcp_servers(second, str(tmp_path))
        registered.set()

    registration = threading.Thread(target=register)
    try:
        assert closing.wait(5)
        registration.start()
        assert registered.wait(5), "client shutdown blocked another registry"
    finally:
        resume.set()
        release.join(5)
        registration.join(5)
        loader.release_mcp_registration(second)


@pytest.mark.parametrize("reuse", ["none", "active", "released"])
@pytest.mark.parametrize("fail_after_operation", [False, True])
@pytest.mark.parametrize("timeout_first", [False, True])
@pytest.mark.parametrize("blocked_operation", ["connect", "listing"])
def test_retired_worker_cannot_orphan_a_real_sync_loop(
    monkeypatch, tmp_path, reuse, fail_after_operation, timeout_first, blocked_operation,
):
    """Retire after admission but before the real client's loop has started."""
    client = MCPClient(MCPClientConfig(transport="stdio", command="unused"))
    entered, resume, finished = threading.Event(), threading.Event(), threading.Event()
    worker_finished = threading.Event()
    loop_threads = []

    async def offline_connect():
        loop_threads.append(threading.current_thread())
        return object()

    monkeypatch.setattr(client, "connect", offline_connect)
    real_connect = client.connect_sync
    real_close = client.close_sync

    def paused_operation(operation):
        entered.set()
        assert resume.wait(5)
        result = operation()
        if fail_after_operation:
            raise RuntimeError("connected, then failed")
        return result

    if blocked_operation == "connect":
        monkeypatch.setattr(client, "connect_sync", lambda: paused_operation(real_connect))
    close = MagicMock(wraps=real_close)
    monkeypatch.setattr(client, "close_sync", close)
    listed = fake_client().list_tools_sync.return_value

    async def offline_listing():
        return listed

    def list_tools():
        operation = lambda: client._run_sync(offline_listing())
        if blocked_operation == "listing" and not entered.is_set():
            return paused_operation(operation)
        return operation()

    monkeypatch.setattr(client, "list_tools_sync", list_tools)
    configure(monkeypatch, iter([client, client]), mode="manual")
    original_background = loader._background_connect

    def background(*args, **kwargs):
        try:
            return original_background(*args, **kwargs)
        finally:
            finished.set()

    monkeypatch.setattr(loader, "_background_connect", background)
    original_thread = threading.Thread

    def instrument_worker(*args, **kwargs):
        target = kwargs.get("target")
        if getattr(target, "__name__", "") == "_work":
            def work():
                try:
                    target()
                finally:
                    worker_finished.set()
            kwargs["target"] = work
        return original_thread(*args, **kwargs)

    monkeypatch.setattr(loader.threading, "Thread", instrument_worker)
    if timeout_first:
        monkeypatch.setattr(loader, "_DEFAULT_CONNECT_TIMEOUT", 0)
    registry = ToolRegistry()
    loader.register_mcp_servers(registry, str(tmp_path))
    try:
        assert entered.wait(5)
        if timeout_first:
            assert finished.wait(5)
            assert loader.get_pending_server_names(registry) == ["shared"]
        if blocked_operation == "connect":
            assert client._sync_loop is None
        assert loader.release_mcp_registration(registry)
        assert close.call_count == 0, "an admitted operation still owns the client"
        if reuse != "none":
            monkeypatch.setattr(loader, "connect_mode", lambda: "startup")
            assert loader.register_mcp_servers(registry, str(tmp_path))[0] == [client]
            if reuse == "released":
                assert loader.release_mcp_registration(registry)
        resume.set()
        assert worker_finished.wait(5)
        assert finished.wait(5)
        if reuse == "active":
            assert close.call_count == 0, "old worker closed a newer generation's client"
            assert client._sync_loop.is_running()
            assert registry.get_tool("shared_read") is not None
            assert loader.release_mcp_registration(registry)
        assert registry not in loader._shared_users
        assert (registry, "shared") not in loader._shared_tools_meta
        assert loader.get_pending_server_names(registry) == []
        assert client._sync_loop is None, "retired worker orphaned its sync loop"
        assert client._sync_thread is None
        assert client not in loader._client_lifetimes
        assert loop_threads and all(not thread.is_alive() for thread in loop_threads)
        assert close.call_count == 1
    finally:
        resume.set()
        assert finished.wait(5)
        loader.release_mcp_registration(registry)
        real_close()


def test_same_client_cannot_be_adopted_while_its_close_is_in_progress(monkeypatch, tmp_path):
    client = fake_client()
    configure(monkeypatch, iter([client, client, client]))
    registry = ToolRegistry()
    loader.register_mcp_servers(registry, str(tmp_path))
    closing, resume = threading.Event(), threading.Event()

    def close():
        closing.set()
        assert resume.wait(5)

    client.close_sync.side_effect = close
    release = threading.Thread(target=loader.release_mcp_registration, args=(registry,))
    release.start()
    try:
        assert closing.wait(5)
        assert loader.register_mcp_servers(registry, str(tmp_path)) == ([], [])
        assert registry not in loader._shared_users
        assert registry.get_tool("shared_read") is None
    finally:
        resume.set()
        release.join(5)
    assert not release.is_alive()
    assert client.close_sync.call_count == 1
    assert client not in loader._client_lifetimes
    # Once closing has completed, this same object may be used in a new lifetime.
    try:
        assert loader.register_mcp_servers(registry, str(tmp_path))[0] == [client]
    finally:
        loader.release_mcp_registration(registry)
    assert client.close_sync.call_count == 2


def offline_runtime_client(monkeypatch):
    """Keep a real persistent loop while replacing only remote coroutines."""
    client = MCPClient(MCPClientConfig(transport="stdio", command="unused"))
    loop_threads = []
    listed = fake_client().list_tools_sync.return_value

    async def connect():
        loop_threads.append(threading.current_thread())
        return object()

    async def listing():
        loop_threads.append(threading.current_thread())
        return listed

    monkeypatch.setattr(client, "connect", connect)
    monkeypatch.setattr(client, "list_tools_sync", lambda: client._run_sync(listing()))
    real_close = client.close_sync
    close = MagicMock(wraps=real_close)
    monkeypatch.setattr(client, "close_sync", close)
    return client, close, real_close, loop_threads


def test_ownerless_manual_registration_closes_under_sandbox_seal(monkeypatch, tmp_path):
    client, close, real_close, loop_threads = offline_runtime_client(monkeypatch)
    entered, resume, finished = threading.Event(), threading.Event(), threading.Event()
    original_listing = client.list_tools_sync

    def listing():
        entered.set()
        assert resume.wait(5)
        return original_listing()

    original_background = loader._background_connect

    def background(*args, **kwargs):
        try:
            return original_background(*args, **kwargs)
        finally:
            finished.set()

    monkeypatch.setattr(client, "list_tools_sync", listing)
    monkeypatch.setattr(loader, "_background_connect", background)
    configure(monkeypatch, iter([client]), mode="manual")
    registry = ToolRegistry()
    registry.register_function("Bash", "Sandbox shell", lambda _: "ok", owner="builtin")
    unregister = MagicMock(wraps=registry.unregister_owner)
    monkeypatch.setattr(registry, "unregister_owner", unregister)
    loader.register_mcp_servers(registry, str(tmp_path))
    try:
        assert entered.wait(5)
        registry.restrict_to_tools({"Bash"})
        resume.set()
        assert finished.wait(5)
        assert registry.registered_owners(prefix="mcp:") == ()
        assert loader.get_pending_server_names(registry) == ["shared"]
        assert client._sync_loop.is_running()
        assert loader.release_mcp_registration(registry)
        unregister.assert_not_called()
        assert close.call_count == 1
        assert client._sync_loop is None and client._sync_thread is None
        assert loop_threads and all(not thread.is_alive() for thread in loop_threads)
        assert client not in loader._client_lifetimes
        assert loader.get_pending_server_names(registry) == []
        assert registry.list_tools() == ["Bash"]
        with pytest.raises(PermissionError, match="sandbox policy"):
            registry.unregister_owner("mcp:shared")
    finally:
        resume.set()
        assert finished.wait(5)
        real_close()


def test_sealed_actual_owner_failure_closes_runtime_and_keeps_sandbox_denial(monkeypatch, tmp_path):
    client, close, real_close, loop_threads = offline_runtime_client(monkeypatch)
    configure(monkeypatch, iter([client]))
    registry = ToolRegistry()
    loader.register_mcp_servers(registry, str(tmp_path))
    snapshot = registry.capture_tool_step()
    registry.restrict_to_tools({"Bash"})
    try:
        with pytest.raises(PermissionError, match="sandbox policy"):
            loader.release_mcp_registration(registry)
        assert close.call_count == 1
        assert client._sync_loop is None and client._sync_thread is None
        assert loop_threads and all(not thread.is_alive() for thread in loop_threads)
        assert client not in loader._client_lifetimes
        assert registry not in loader._shared_users
        # Structural policy remains sealed; failure is propagated instead of
        # claiming the hidden adapter was removed. Even its old snapshot denies.
        assert registry.registered_owners(prefix="mcp:") == ("mcp:shared",)
        assert registry.get_openai_tools() == []
        response = json.loads(registry.execute_tool("shared_read", {}, snapshot=snapshot))
        assert response["error"]["code"].upper() == "ACCESS_DENIED"
        with pytest.raises(PermissionError):
            registry.restrict_to_tools({"Bash", "shared_read"})
    finally:
        real_close()


def test_owner_invalidation_failure_still_retires_other_owned_tools_and_clients(monkeypatch, tmp_path):
    first, second = fake_client(), fake_client()
    configure(monkeypatch, iter([first, second]))
    monkeypatch.setattr(loader, "load_mcp_servers", lambda root: {
        "shared": {"command": "echo"}, "second": {"command": "echo"},
    })
    registry = ToolRegistry()
    registry.register_function("external", "Unrelated owner", lambda _: "ok", owner="mcp:external")
    external = registry.get_function("external")
    loader.register_mcp_servers(registry, str(tmp_path))
    snapshot = registry.capture_tool_step()
    original_unregister = registry.unregister_owner

    def unregister(owner):
        if owner == "mcp:shared":
            raise RuntimeError("owner invalidation failed")
        return original_unregister(owner)

    monkeypatch.setattr(registry, "unregister_owner", unregister)
    with pytest.raises(RuntimeError, match="owner invalidation failed"):
        loader.release_mcp_registration(registry)
    assert first.close_sync.call_count == second.close_sync.call_count == 1
    assert first not in loader._client_lifetimes and second not in loader._client_lifetimes
    assert registry.get_tool("second_read") is None
    response = json.loads(registry.execute_tool("second_read", {}, snapshot=snapshot))
    assert response["error"]["code"] == "STALE_TOOL_SCHEMA"
    assert registry.get_function("external") is external
    assert registry.owner_of("external") == "mcp:external"
    assert registry.owner_of("shared_read") == "mcp:shared"


def test_owner_enumeration_failure_still_closes_claimed_client(monkeypatch, tmp_path):
    client = fake_client()
    configure(monkeypatch, iter([client]))
    registry = ToolRegistry()
    loader.register_mcp_servers(registry, str(tmp_path))
    monkeypatch.setattr(registry, "registered_owners", MagicMock(side_effect=RuntimeError("owner lookup failed")))
    with pytest.raises(RuntimeError, match="owner lookup failed"):
        loader.release_mcp_registration(registry)
    assert client.close_sync.call_count == 1
    assert client not in loader._client_lifetimes
