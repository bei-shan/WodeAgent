"""Opt-in *billable* live E2B cloud sandbox acceptance.

Requires an operator-managed isolated test project, the optional SDK, an
active E2B credential, and two explicit acknowledgements. Not regular CI.
"""
from __future__ import annotations

import os
import secrets

import pytest

from core.sandbox.e2b_provider import E2BCommandProvider
from core.sandbox.e2b_guest_files import E2BGuestFilesystem

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


def test_live_remote_guest_read_and_list_without_host_export(tmp_path):
    """Explicitly billable E2B integration; skipped in normal CI."""
    if not os.getenv("E2B_API_KEY"):
        pytest.skip("operator E2B_API_KEY is not set")
    provider = E2BCommandProvider(
        tmp_path, sandbox_ttl=180, capacity_limit=1,
        single_gateway_ack=True,
    )
    name = "ridge-live-" + secrets.token_hex(8) + ".bin"
    try:
        result = provider.execute(
            f"python3 -c 'from pathlib import Path; Path(\"{name}\").write_bytes(bytes([0,255,3]))'",
            timeout_seconds=60,
        )
        assert result.exit_code == 0, result.stderr
        files = E2BGuestFilesystem(provider)
        assert files.read_file(name, timeout_seconds=60) == b"\x00\xff\x03"
        assert name in files.list_dir(".", timeout_seconds=60)
        with pytest.raises(PermissionError):
            files.read_file("../etc/passwd")
        assert not (tmp_path / name).exists()
    finally:
        provider.close()
