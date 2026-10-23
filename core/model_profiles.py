"""Model profile registry — environment variables plus UI-managed profiles.

Supports Claude Code-style model switching.  Each profile defines a
model identifier plus its provider, API key, and base URL.

Profiles come from two places:

* environment (``.env``), for operators who want nothing written to disk;
* ``<data_dir>/settings/models.json``, written by the Web settings panel.

A profile defined in the file wins over an environment profile with the same
name, so editing a key in the UI takes effect without restarting the process.

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

import json
import os
import re
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

PROFILE_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")


def data_dir() -> Path:
    """Local data root shared with the Web service."""
    override = os.getenv("MYCODEAGENT_DATA_DIR", "").strip()
    return Path(override) if override else Path.cwd() / ".mycodeagent"


def profiles_path() -> Path:
    return data_dir() / "settings" / "models.json"


def _clean_entry(entry: Any) -> Optional[Dict[str, Optional[str]]]:
    if not isinstance(entry, dict):
        return None
    model = str(entry.get("model") or "").strip()
    if not model:
        return None
    return {
        "model": model,
        "provider": str(entry.get("provider") or "").strip() or None,
        "api_key": str(entry.get("api_key") or "").strip() or None,
        "base_url": str(entry.get("base_url") or "").strip() or None,
    }


def load_saved_profiles() -> Dict[str, Dict[str, Optional[str]]]:
    """Profiles persisted by the settings panel; empty when the file is bad."""
    path = profiles_path()
    if not path.is_file():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    entries = raw.get("profiles") if isinstance(raw, dict) else None
    if not isinstance(entries, dict):
        return {}
    profiles: Dict[str, Dict[str, Optional[str]]] = {}
    for name, entry in entries.items():
        key = str(name).strip().lower()
        if not PROFILE_NAME_RE.match(key):
            continue
        cleaned = _clean_entry(entry)
        if cleaned is None:
            continue
        profiles[key] = cleaned
    return profiles


def save_saved_profiles(profiles: Dict[str, Dict[str, Optional[str]]]) -> None:
    path = profiles_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        tmp.write_text(
            json.dumps({"profiles": profiles}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def upsert_profile(
    name: str,
    model: str,
    *,
    provider: str | None = None,
    api_key: str | None = None,
    base_url: str | None = None,
) -> Dict[str, Optional[str]]:
    """Create or update a profile; an empty api_key keeps the stored key."""
    key = str(name or "").strip().lower()
    if not PROFILE_NAME_RE.match(key):
        raise ValueError("Profile name must be 1-32 chars: lowercase letters, digits, - or _")
    model_id = str(model or "").strip()
    if not model_id:
        raise ValueError("Model id is required")
    profiles = load_saved_profiles()
    previous = profiles.get(key, {})
    stored_key = api_key if api_key not in (None, "") else previous.get("api_key")
    profiles[key] = {
        "model": model_id,
        "provider": (provider or "").strip() or previous.get("provider"),
        "api_key": stored_key or None,
        "base_url": (base_url or "").strip() or previous.get("base_url"),
    }
    save_saved_profiles(profiles)
    return profiles[key]


def delete_profile(name: str) -> bool:
    key = str(name or "").strip().lower()
    profiles = load_saved_profiles()
    if key not in profiles:
        return False
    del profiles[key]
    save_saved_profiles(profiles)
    return True


def profile_source(name: str) -> str:
    """Where a profile comes from: ``file`` (UI), ``env``, or ``none``."""
    key = str(name or "").strip().lower()
    if key in load_saved_profiles():
        return "file"
    env_names = {
        raw.strip().lower() for raw in os.getenv("MODEL_PROFILES", "").split(",") if raw.strip()
    }
    return "env" if key in env_names else "none"


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
        masked = None
        if self.api_key:
            masked = "***" + self.api_key[-4:] if len(self.api_key) >= 12 else "***"
        return {
            "name": self.name,
            "model": self.model,
            "provider": self.provider,
            "api_key": masked,
            "has_key": bool(self.api_key),
            "base_url": self.base_url,
            "source": profile_source(self.name),
        }


def load_model_profiles() -> Dict[str, ModelProfile]:
    """Load profiles from the environment and the UI-managed settings file.

    Reads ``MODEL_PROFILES`` (comma-separated names), then for each name looks
    up ``MODEL_<NAME>_ID``, ``MODEL_<NAME>_PROVIDER``, etc. File-backed profiles
    are applied afterwards and win on name collisions.

    Returns a dict keyed by lowercase profile name.
    """
    profiles: Dict[str, ModelProfile] = {}
    names = os.getenv("MODEL_PROFILES", "")
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

    for name, entry in load_saved_profiles().items():
        profiles[name] = ModelProfile(
            name=name,
            model=entry["model"] or "",
            provider=entry.get("provider"),
            api_key=entry.get("api_key"),
            base_url=entry.get("base_url"),
        )
    return profiles


def list_model_profiles(profiles: Dict[str, ModelProfile]) -> List[dict]:
    """Return a human-readable list of available profiles."""
    return [p.as_dict() for p in profiles.values()]


# ------------------------------------------------------------------
# Model pointers — automatic model selection by use-case
# ------------------------------------------------------------------

def resolve_model_pointer(pointer: str) -> ModelProfile | None:
    """Resolve a logical pointer (main/task/compact/quick) to a profile.

    Reads ``MODEL_POINTER_MAIN``, ``MODEL_POINTER_TASK``, etc. from
    environment.  Each value should be a profile name registered in
    ``MODEL_PROFILES``.

    Returns None if the pointer is not configured or the profile is
    not found.

    Example::

        MODEL_PROFILES=opus,haiku
        MODEL_OPUS_ID=claude-opus-4-8
        MODEL_HAIKU_ID=claude-haiku-4-5

        MODEL_POINTER_MAIN=opus
        MODEL_POINTER_TASK=haiku
        MODEL_POINTER_COMPACT=haiku
    """
    pointer_key = f"MODEL_POINTER_{pointer.upper()}"
    profile_name = os.getenv(pointer_key, "").strip().lower()
    if not profile_name:
        return None
    profiles = load_model_profiles()
    return profiles.get(profile_name)


def create_llm_from_pointer(
    pointer: str,
    *,
    fallback_llm,  # HelloAgentsLLM — used if pointer not configured
    temperature: float | None = None,
) -> Any:
    """Create a HelloAgentsLLM from a model pointer.

    If the pointer resolves to a profile, use the profile's credentials.
    Otherwise fall back to *fallback_llm* (the main agent's LLM).
    """
    profile = resolve_model_pointer(pointer)
    if profile is None:
        return fallback_llm

    from core.llm import HelloAgentsLLM

    return HelloAgentsLLM(
        model=profile.model,
        api_key=profile.api_key or fallback_llm.api_key,
        base_url=profile.base_url or fallback_llm.base_url,
        provider=profile.provider or fallback_llm.provider,
        temperature=temperature if temperature is not None else fallback_llm.temperature,
        max_tokens=fallback_llm.max_tokens,
        timeout=fallback_llm.timeout,
    )
