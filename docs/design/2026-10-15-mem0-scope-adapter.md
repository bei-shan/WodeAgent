# Mem0 opt-in Memory provider (2026-10-15)

> **Experimental SDK adapter only.** Not wired into the Web Settings UI,
> automatic Agent prompt recall/capture, tenant credential vault or async
> persistence. Do not advertise as a production-enabled cloud provider.

`core/memory/mem0_backend.py` supports the documented Mem0 OSS v3
`add(messages, user_id=..., infer=False)`,
`search(query, filters={"user_id": ...}, top_k=N)`,
`get(id)`, and `delete(id)` methods. It does not automatically import
the SDK or establish any network connection. An operator supplies a **trusted
configured SDK instance** and explicitly acknowledges content export.

Example in a **trusted local Python worker only**:

```python
from mem0 import Memory
from core.memory import Mem0MemoryBackend, MemoryScope, MemoryProviderRegistry

# Credentials, embeddings, LLM and storage configuration are operator owned;
# never expose them to a browser, prompt or user-provided tool parameter.
client = Memory.from_config({...})
backend = Mem0MemoryBackend(
    client,
    scope_key=bytes.fromhex("<64-or-more-hex-digit-secret-from-vault>"),
    approved=True,
)
registry = MemoryProviderRegistry({"mem0": backend})
scope = MemoryScope(tenant_id="trusted-tenant", user_id="authenticated-user",
                    project_id="approved-project", agent_id="coding-agent")
registry.select(scope, "mem0")
```

This is illustration; configure the Mem0 SDK with a private data plane
and verified per-tenant authorization before use. **Do not paste secrets or
models into the GitHub repository.**

Isolation model:

- A persistent HMAC-SHA256 key transforms the four trusted dimensions
  (tenant/user/project/agent) into a single opaque Mem0 `user_id`.
  It prevents identical raw user IDs in different tenants from colliding
  and avoids exporting plain tenant names.
- Searches explicitly filter the opaque ID and **post-filter every returned
  record**. Remote SDK replies missing owner identity are excluded.
- Delete fetches and verifies the remote record's owner before deletion.
  It never calls `delete_all` and never trusts a model-provided ID alone.
- Add uses `infer=False`, so automatic fact extraction/LLM calls cannot
  happen without a separately designed and consented pipeline. The
  add result must be confirmed via a Mem0 `get` when the SDK omits owner.
- Metadata cannot contain ownership overrides. Backend registration and
  provider selection are both opt-in; default remains OFF.
- If the HMAC key rotates, old remote records become unreachable until
  explicitly migrated with a secure provenance-preserving process.
- This HMAC mapping **does not replace Mem0 backend ACL** or the trusted
  principal/membership lookup. Provider credentials must also be separated
  per trust boundary.

Required before enabling the UI/provider in production: real Mem0 SDK
compatibility tests, remote server permission validation, selective
migration/export/delete, external data transfer consent, key rotation,
prompt injection tests, quota/timeout/retry/latency metrics and per-tenant
credential vault integration. See `docs/releases/hosted-production-acceptance.md`.
