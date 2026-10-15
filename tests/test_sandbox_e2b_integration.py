"""Opt-in *billable* live E2B cloud sandbox acceptance.

Requires an operator-managed isolated test project, the optional SDK, an
active E2B credential, and two explicit acknowledgements. Not regular CI.
"""
from __future__ import annotations

import os
import secrets

import pytest

from core.sandbox.e2b_provider import E2BCommandProvider

pytestmark = pytest.mark.skipif(
    (os.getenv("AGENT_E2B_INTEGRATION") != "1"
     or os.getenv("AGENT_E2B_ALLOW_BILLING") != "1"),
    reason="live E2B VM requires explicit credential and cloud billing approval",
)


def test_live_e2b_remote_vm_no_local_workspace_sync(tmp_path):
    if not os.getenv("E2B_API_KEY"):
        pytest.skip("operator E2B_API_KEY is not set")
    marker = f"ridge-e2b-{secrets.token_hex(12)}.txt"
    (tmp_path / marker).write_text("never-sync-host-content", encoding="utf-8")
    provider = E2BCommandProvider(
        tmp_path,
        template=os.getenv("AGENT_SANDBOX_E2B_TEMPLATE", "code-interpreter-v1"),
        sandbox_ttl=180,
        capacity_limit=1,
        single_gateway_ack=True,
    )
    try:
        read = provider.execute(f"test ! -e {marker}; pwd", timeout_seconds=60)
        assert read.exit_code == 0, read.stderr
        assert read.stdout.strip() == "/home/user"
        write = provider.execute(
            f"printf 'guest-only' > {marker}; cat {marker}",
            timeout_seconds=60,
        )
        assert write.exit_code == 0, write.stderr
        assert write.stdout.strip() == "guest-only"
        assert (tmp_path / marker).read_text() == "never-sync-host-content"
        assert provider.capacity_used == 1
    finally:
        provider.close()
    assert provider.capacity_used == 0
