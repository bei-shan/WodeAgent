"""Minimal inherited subprocess environment for untrusted tool execution.

This limits accidental credential inheritance, not filesystem/network access.
Never rely on env filtering to replace a sandbox or command authorization.
"""
from __future__ import annotations

import os
from collections.abc import Mapping

# Explicit allowlist: do not inherit API keys, cloud credentials, dynamic loader
# hooks, Python/Node startup hooks, git config injection, or proxy credentials.
_INHERITED = frozenset({
    "PATH", "PATHEXT", "SYSTEMROOT", "WINDIR", "COMSPEC",
    "LANG", "LANGUAGE", "LC_ALL", "LC_CTYPE", "TZ",
    "TERM", "NO_COLOR", "CI",
})


def safe_child_env(
    explicit: Mapping[str, str] | None = None,
    *,
    home: str | None = None,
) -> dict[str, str]:
    """Return strictly allowlisted ambient variables plus deliberate overrides.

    Explicit variables represent a deliberate trusted application grant. MCP
    config/env and subprocess tools MUST NOT let model arguments populate it.
    """
    env = {
        name: value for name, value in os.environ.items()
        if name.upper() in _INHERITED and isinstance(value, str)
    }
    env.setdefault("PATH", os.defpath)
    if home is not None:
        env.update({
            "HOME": home,
            "XDG_CONFIG_HOME": home,
            "XDG_CACHE_HOME": home,
            "TMPDIR": home,
            "TEMP": home,
            "TMP": home,
        })
    if explicit is not None:
        if not isinstance(explicit, Mapping):
            raise TypeError("Explicit tool environment must be a mapping")
        for name, value in explicit.items():
            if not isinstance(name, str) or not name or "\x00" in name or "=" in name:
                raise ValueError("Invalid explicit subprocess environment name")
            if not isinstance(value, str) or "\x00" in value:
                raise ValueError("Invalid explicit subprocess environment value")
            env[name] = value
    return env
