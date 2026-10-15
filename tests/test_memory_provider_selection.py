"""Contract tests for the opt-in Memory foundation; no provider API keys."""

from concurrent.futures import ThreadPoolExecutor

import pytest

from core.memory import (
    MemoryProviderRegistry,
    MemoryProviderUnavailable,
    MemoryScope,
    SQLiteMemoryBackend,
)


def scope(tenant="t1", user="u1", project="p1", agent="a1"):
    return MemoryScope(tenant_id=tenant, user_id=user, project_id=project, agent_id=agent)


@pytest.mark.parametrize(
    "values",
    [
        {"tenant_id": "", "user_id": "u1", "project_id": "p1"},
        {"tenant_id": "t1", "user_id": "", "project_id": "p1"},
        {"tenant_id": "t1", "user_id": "u1", "project_id": ""},
        {"tenant_id": "../other", "user_id": "u1", "project_id": "p1"},
        {"tenant_id": "t1", "user_id": "u1", "project_id": "p1", "agent_id": "bad/path"},
    ],
)
def test_rejects_missing_or_path_like_identity(values):
    with pytest.raises(ValueError):
        MemoryScope(**values)


def test_all_four_identity_dimensions_apply_to_search_and_delete(tmp_path):
    backend = SQLiteMemoryBackend(tmp_path / "memory.db")
    owner = scope()
    record = backend.add(owner, "prefer deterministic tests", metadata={"source": "manual"})
    assert record.metadata["source"] == "manual"

    for other in [
        scope(tenant="t2"),
        scope(user="u2"),
        scope(project="p2"),
        scope(agent="a2"),
    ]:
        assert backend.search(other, "deterministic") == []
        assert backend.delete(other, record.id) is False

    assert [m.id for m in backend.search(owner, "deterministic")] == [record.id]
    assert backend.delete(owner, record.id) is True
    assert backend.search(owner, "") == []


def test_search_literal_wildcards_no_sql_injection(tmp_path):
    backend = SQLiteMemoryBackend(tmp_path / "memory.db")
    owner = scope()
    backend.add(owner, "alpha 100% done")
    backend.add(owner, "beta_b")
    assert [m.content for m in backend.search(owner, "%")] == ["alpha 100% done"]
    assert [m.content for m in backend.search(owner, "_")] == ["beta_b"]
    assert backend.search(owner, "' OR 1=1 --") == []


def test_search_has_hard_bounds(tmp_path):
    backend = SQLiteMemoryBackend(tmp_path / "memory.db")
    with pytest.raises(ValueError):
        backend.search(scope(), "query", limit=100000)
    with pytest.raises(ValueError):
        backend.search(scope(), "q" * 2049)
    with pytest.raises(ValueError):
        backend.add(scope(), " ")
    with pytest.raises(ValueError):
        backend.add(scope(), "x", metadata={"oversized": "s" * 5000})


def test_concurrent_writes_remain_readable(tmp_path):
    backend = SQLiteMemoryBackend(tmp_path / "memory.db")
    with ThreadPoolExecutor(max_workers=8) as pool:
        ids = list(pool.map(lambda i: backend.add(scope(), f"item {i}").id, range(32)))
    assert len(set(ids)) == 32
    assert len(backend.search(scope(), "item", limit=50)) == 32


def test_select_off_by_default_and_fail_closed_on_missing_adapters(tmp_path):
    registry = MemoryProviderRegistry({"sqlite": SQLiteMemoryBackend(tmp_path / "db.sqlite")})
    alice, bob = scope(user="alice"), scope(user="bob")
    assert registry.selected(alice) == "off"
    assert registry.for_scope(alice).search(alice, "anything") == []
    with pytest.raises(MemoryProviderUnavailable):
        registry.for_scope(alice).add(alice, "should not persist")
    for name in ("mem0", "openviking", "tencentdb", "nonexistent"):
        with pytest.raises(MemoryProviderUnavailable):
            registry.select(alice, name)
    assert registry.selected(alice) == "off"

    registry.select(alice, "sqlite")
    assert registry.selected(alice) == "sqlite"
    assert registry.selected(bob) == "off"
    assert registry.status() == {
        "off": True, "sqlite": True, "mem0": False,
        "openviking": False, "tencentdb": False,
    }
    backend = registry.for_scope(alice)
    backend.add(alice, "a preference")
    assert backend.search(alice, "preference")
    with pytest.raises(PermissionError):
        backend.search(bob, "preference")
    with pytest.raises(PermissionError):
        backend.add(bob, "attempted cross-scope write")
    with pytest.raises(PermissionError):
        backend.delete(bob, "fake-other-record")
    registry.select(alice, "off")
    # A cached handle can never bypass revocation by retaining the old
    # SQLite/Mem0 backend reference after a user disables memory.
    assert backend.search(alice, "preference") == []
    with pytest.raises(MemoryProviderUnavailable):
        backend.add(alice, "must remain disabled")
    assert registry.for_scope(alice).search(alice, "preference") == []
    registry.select(alice, "sqlite")
    assert [r.content for r in backend.search(alice, "preference")] == ["a preference"]



def test_concurrent_memory_provider_toggle_never_permits_cross_scope(tmp_path):
    registry = MemoryProviderRegistry({"sqlite": SQLiteMemoryBackend(tmp_path / "db.sqlite")})
    alice = scope(user="alice")
    bob = scope(user="bob")
    registry.select(alice, "sqlite")
    handle = registry.for_scope(alice)
    handle.add(alice, "private scope only")

    def probe(index):
        if index % 3 == 0:
            registry.select(alice, "off")
        elif index % 3 == 1:
            registry.select(alice, "sqlite")
        try:
            handle.search(bob, "private")
        except PermissionError:
            return True
        return False

    with ThreadPoolExecutor(max_workers=8) as pool:
        assert all(pool.map(probe, range(64)))
