"""Opt-in Mem0 adapter: strict tenant isolation and no external API calls."""

import pytest

from core.memory import MemoryProviderRegistry, MemoryScope
from core.memory.mem0_backend import Mem0MemoryBackend


class FakeMem0:
    def __init__(self):
        self.rows = {}
        self.invocations = []
        self.next_id = 0
        self.inject_foreign = False

    def add(self, content, *, user_id, infer, metadata):
        self.invocations.append(("add", user_id, infer))
        self.next_id += 1
        rid = str(self.next_id)
        self.rows[rid] = {
            "id": rid, "memory": content, "user_id": user_id, "metadata": dict(metadata),
        }
        # The OSS add response may omit user_id.
        return {"results": [{"id": rid, "memory": content, "event": "ADD"}]}

    def get(self, memory_id):
        return self.rows.get(memory_id)

    def search(self, query, *, filters, top_k):
        self.invocations.append(("search", filters["user_id"], top_k))
        rows = [r for r in self.rows.values() if r["user_id"] == filters["user_id"]
                and query.lower() in r["memory"].lower()]
        if self.inject_foreign:
            rows.append({"id": "foreign", "memory": "foreign leak", "user_id": "evil"})
        return {"results": rows}

    def delete(self, memory_id):
        self.rows.pop(memory_id)


def sc(tenant="one", user="user", project="project", agent="agent"):
    return MemoryScope(tenant_id=tenant, user_id=user, project_id=project, agent_id=agent)


def test_opt_in_and_persistent_secret_are_required():
    with pytest.raises(PermissionError):
        Mem0MemoryBackend(FakeMem0(), scope_key=b"x" * 32)
    with pytest.raises(ValueError):
        Mem0MemoryBackend(FakeMem0(), scope_key=b"weak", approved=True)


def test_opaque_user_id_uses_all_scope_dimensions_and_confirms_owner():
    client = FakeMem0()
    b = Mem0MemoryBackend(client, scope_key=b"secret_key" * 5, approved=True)
    alice = sc()
    record = b.add(alice, "prefers small diffs", metadata={"source": "manual"})
    assert record.content == "prefers small diffs"
    assert record.scope == alice
    key = client.invocations[0][1]
    assert key.startswith("ridge_")
    assert all(raw not in key for raw in ("one", "user", "project", "agent"))
    assert client.invocations[0][2] is False  # Never infer without permission.

    for other in (sc(tenant="two"), sc(user="another"), sc(project="other"), sc(agent="other")):
        assert b.search(other, "diffs") == []
        assert not b.delete(other, record.id)

    client.inject_foreign = True
    assert [x.id for x in b.search(alice, "diffs")] == [record.id]
    assert b.delete(alice, record.id)
    assert b.search(alice, "diffs") == []


def test_payload_identity_overrides_are_rejected():
    client = FakeMem0()
    b = Mem0MemoryBackend(client, scope_key=b"z" * 32, approved=True)
    for forbidden in ("tenant_id", "user_id", "project_id", "agent_id", "run_id"):
        with pytest.raises(ValueError):
            b.add(sc(), "content", metadata={forbidden: "attacker"})
    assert client.invocations == []


def test_fail_closed_if_sdk_returns_wrong_persisted_owner():
    client = FakeMem0()
    client.get = lambda _: {"id": "1", "memory": "leak", "user_id": "other"}
    b = Mem0MemoryBackend(client, scope_key=b"x" * 32, approved=True)
    with pytest.raises(ValueError, match="ownership"):
        b.add(sc(), "do not trust response")


def test_registry_disables_mem0_until_operator_explicitly_installs():
    registry = MemoryProviderRegistry()
    assert not registry.status()["mem0"]
    with pytest.raises(Exception):
        registry.select(sc(), "mem0")
    backend = Mem0MemoryBackend(FakeMem0(), scope_key=b"w" * 32, approved=True)
    registry.register("mem0", backend)
    registry.select(sc(), "mem0")
    assert registry.for_scope(sc()) is backend
