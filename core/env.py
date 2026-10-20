"""Environment loader helper to ensure .env is applied early."""

import os
from typing import Optional


_ENV_LOADED = False
# Runtime dotenv values are configuration hints, NOT operator-trust evidence.
_DOTENV_INJECTED: set[str] = set()


def load_env() -> None:
    """Load .env once if available (no override)."""
    global _ENV_LOADED
    if _ENV_LOADED:
        return
    before = set(os.environ)
    try:
        from dotenv import load_dotenv, find_dotenv
        dotenv_path = find_dotenv(usecwd=True)
        if dotenv_path:
            load_dotenv(dotenv_path, override=False)
        else:
            load_dotenv(override=False)
    except Exception:
        # Fail silently: rely on system environment if dotenv is missing.
        pass
    _DOTENV_INJECTED.update(set(os.environ) - before)
    _ENV_LOADED = True


def get_operator_env(key: str) -> Optional[str]:
    """Read a runtime/process setting but not a workspace .env trust grant.

    The centralized dotenv loader records names it introduced, so a malicious
    repository's .env cannot turn on executable MCP config trust or provide
    a fake operator-supplied MCP_SERVERS override.
    """
    load_env()
    if key in _DOTENV_INJECTED:
        return None
    return os.environ.get(key)


def getenv(key: str, default: Optional[str] = None) -> Optional[str]:
    """Get env var after ensuring .env is loaded."""
    load_env()
    return os.getenv(key, default)
