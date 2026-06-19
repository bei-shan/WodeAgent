"""Model profile registry — loads available models from environment.

Supports Claude Code-style model switching.  Each profile defines a
model identifier plus its provider, API key, and base URL.

Configuration via ``.env``::

    # Register available profiles (comma-separated names)
    MODEL_PROFILES=deepseek,gpt4o,claude

    # Profile: deepseek
    MODEL_DEEPSEEK_ID=deepseek-v4-pro
    MODEL_DEEPSEEK_PROVIDER=deepseek
    MODEL_DEEPSEEK_API_KEY=sk-xxx
    MODEL_DEEPSEEK_BASE_URL=https://api.deepseek.com

    # Profile: gpt4o
    MODEL_GPT4O_ID=gpt-4o
    MODEL_GPT4O_PROVIDER=openai
    MODEL_GPT4O_API_KEY=sk-yyy
    MODEL_GPT4O_BASE_URL=https://api.openai.com/v1

    # Profile: claude (via proxy)
    MODEL_CLAUDE_ID=claude-sonnet-4-6
    MODEL_CLAUDE_PROVIDER=openai
    MODEL_CLAUDE_API_KEY=sk-zzz
    MODEL_CLAUDE_BASE_URL=https://proxy.example.com/v1

Each profile is keyed by its **uppercase** name with prefix ``MODEL_``
and suffix ``_ID`` / ``_PROVIDER`` / ``_API_KEY`` / ``_BASE_URL``.

If no profiles are configured, ``/model <raw-id>`` still works using
the current credentials.
"""

from __future__ import annotations

import os
from typing import Any, Dict, List, Optional


class ModelProfile:
    """A named model configuration."""

    __slots__ = ("name", "model", "provider", "api_key", "base_url")

    def __init__(
        self,
        name: str,
        model: str,
        provider: str | None = None,
        api_key: str | None = None,
        base_url: str | None = None,
    ):
        self.name = name
        self.model = model
        self.provider = provider
        self.api_key = api_key
        self.base_url = base_url

    def as_dict(self) -> dict:
        return {
            "name": self.name,
            "model": self.model,
            "provider": self.provider,
            "api_key": self.api_key[:8] + "***" if self.api_key else None,
            "base_url": self.base_url,
        }


def load_model_profiles() -> Dict[str, ModelProfile]:
    """Load model profiles from environment variables.

    Reads ``MODEL_PROFILES`` (comma-separated names), then for each
    name looks up ``MODEL_<NAME>_ID``, ``MODEL_<NAME>_PROVIDER``, etc.

    Returns a dict keyed by lowercase profile name.
    """
    names = os.getenv("MODEL_PROFILES", "")
    if not names.strip():
        return {}

    profiles: Dict[str, ModelProfile] = {}
    for raw in names.split(","):
        name = raw.strip().upper()
        if not name:
            continue
        model = os.getenv(f"MODEL_{name}_ID", "").strip()
        if not model:
            continue
        profile = ModelProfile(
            name=name.lower(),
            model=model,
            provider=os.getenv(f"MODEL_{name}_PROVIDER", "").strip() or None,
            api_key=os.getenv(f"MODEL_{name}_API_KEY", "").strip() or None,
            base_url=os.getenv(f"MODEL_{name}_BASE_URL", "").strip() or None,
        )
        profiles[name.lower()] = profile

    return profiles


def list_model_profiles(profiles: Dict[str, ModelProfile]) -> List[dict]:
    """Return a human-readable list of available profiles."""
    return [p.as_dict() for p in profiles.values()]
