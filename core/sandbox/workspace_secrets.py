"""Fail-closed preflight for obvious credentials exposed by RW workspace mounts.

This is an additive guard, NOT a snapshot, TOCTOU guarantee, DLP or substitute
for an isolated per-run file staging service. A malicious host-side actor can
still mutate a bind-mounted tree between scanning and execution.
"""
from __future__ import annotations

import os
from pathlib import Path

from core.sandbox.contracts import SandboxUnavailable

# Only known secrets/config containers, not generic code/test files.
_FORBIDDEN_NAMES = frozenset({
    ".env", ".npmrc", ".pypirc", ".netrc", ".git-credentials",
    "id_rsa", "id_dsa", "id_ecdsa", "id_ed25519",
    "credentials.json", "service-account.json",
    "service_account.json", "application_default_credentials.json",
})
_FORBIDDEN_DIRS = frozenset({
    ".ssh", ".aws", ".azure", ".kube", ".gnupg", ".secrets",
})
_PUBLIC_ENV_TEMPLATES = frozenset({
    ".env.example", ".env.sample", ".env.template", ".env.defaults",
})
_MAX_ENTRIES = 100_000


def _sensitive(name: str, *, directory: bool) -> bool:
    value = name.casefold()
    if directory:
        return value in _FORBIDDEN_DIRS
    if value in _FORBIDDEN_NAMES:
        return True
    if value.startswith(".env.") and value not in _PUBLIC_ENV_TEMPLATES:
        return True
    if value.endswith((".p12", ".pfx", ".pem")):
        return True
    if value.endswith(".key") and not value.endswith(".pub.key"):
        return True
    return False


def assert_no_mounted_credentials(workspace: Path) -> None:
    """Reject suspicious files before granting a whole-workspace RW mount.

    Recheck on execution as well as provider initialization. Entire workspace
    mounts cannot exclude discovered files; abort instead of pretending that
    they are protected. An unreadable or exceptionally large tree fails closed.
    """
    root = Path(workspace).resolve(strict=True)
    if not root.is_dir():
        raise SandboxUnavailable("Sandbox workspace is not a directory")
    visited = 0

    def onerror(exc: OSError) -> None:
        raise SandboxUnavailable("Cannot safely inspect sandbox workspace") from exc

    try:
        for location, directories, files in os.walk(root, followlinks=False, onerror=onerror):
            # os.walk does not follow symlinked directories by default.
            for name in directories:
                visited += 1
                if _sensitive(name, directory=True):
                    raise SandboxUnavailable(
                        "Workspace contains a credential directory; refusing full mount"
                    )
            for name in files:
                visited += 1
                if _sensitive(name, directory=False):
                    raise SandboxUnavailable(
                        "Workspace contains a credential file; refusing full mount"
                    )
            if visited > _MAX_ENTRIES:
                raise SandboxUnavailable(
                    "Workspace is too large to verify for sensitive bind mounts"
                )
    except OSError as exc:
        raise SandboxUnavailable("Cannot inspect sandbox workspace") from exc
