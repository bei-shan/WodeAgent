"""Search provider catalog and persisted selection.

Tavily is a popular default, not a standard: Brave, Serper, Exa, SearXNG and
DuckDuckGo are all common in agent projects. The provider and its credential
are resolved at call time so a change in the Web settings applies immediately.
"""
from __future__ import annotations

import json
import os
import uuid
from pathlib import Path
from typing import Any

from core.secrets import data_dir, get_secret

DEFAULT_PROVIDER = "tavily"

PROVIDERS: dict[str, dict[str, Any]] = {
    "tavily": {
        "label": "Tavily",
        "key_env": "TAVILY_API_KEY",
        "requires_key": True,
        "requires_url": False,
        "signup": "https://tavily.com",
    },
    "brave": {
        "label": "Brave Search",
        "key_env": "BRAVE_API_KEY",
        "requires_key": True,
        "requires_url": False,
        "signup": "https://brave.com/search/api/",
    },
    "serper": {
        "label": "Serper (Google)",
        "key_env": "SERPER_API_KEY",
        "requires_key": True,
        "requires_url": False,
        "signup": "https://serper.dev",
    },
    "exa": {
        "label": "Exa",
        "key_env": "EXA_API_KEY",
        "requires_key": True,
        "requires_url": False,
        "signup": "https://exa.ai",
    },
    "searxng": {
        "label": "SearXNG (self-hosted)",
        "key_env": "SEARXNG_BASE_URL",
        "requires_key": False,
        "requires_url": True,
        "signup": "https://docs.searxng.org",
    },
    "duckduckgo": {
        "label": "DuckDuckGo (no key)",
        "key_env": None,
        "requires_key": False,
        "requires_url": False,
        "signup": "https://duckduckgo.com",
    },
}


class SearchSettingsError(ValueError):
    """Rejected search settings."""


def settings_path() -> Path:
    return data_dir() / "settings" / "search.json"


def load_search_settings() -> dict[str, str]:
    path = settings_path()
    provider = os.getenv("WEB_SEARCH_PROVIDER", "").strip().lower()
    searxng_url = os.getenv("SEARXNG_BASE_URL", "").strip()
    if path.is_file():
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            raw = {}
        if isinstance(raw, dict):
            provider = provider or str(raw.get("provider") or "").strip().lower()
            searxng_url = searxng_url or str(raw.get("searxng_base_url") or "").strip()
    provider = provider if provider in PROVIDERS else DEFAULT_PROVIDER
    return {"provider": provider, "searxng_base_url": searxng_url}


def save_search_settings(provider: str, searxng_base_url: str = "") -> dict[str, str]:
    name = str(provider or "").strip().lower()
    if name not in PROVIDERS:
        raise SearchSettingsError(f"Unsupported search provider: {provider}")
    url = str(searxng_base_url or "").strip()
    if PROVIDERS[name]["requires_url"]:
        if not url.startswith(("http://", "https://")):
            raise SearchSettingsError("SearXNG requires an http(s) base URL")
    path = settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        tmp.write_text(
            json.dumps({"provider": name, "searxng_base_url": url}, indent=2),
            encoding="utf-8",
        )
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)
    return {"provider": name, "searxng_base_url": url}


def provider_credential(provider: str) -> str | None:
    """API key or base URL for a provider, from keychain/file/env."""
    entry = PROVIDERS.get(provider)
    if not entry or not entry.get("key_env"):
        return None
    return get_secret(entry["key_env"])


def provider_status() -> list[dict[str, Any]]:
    from core.secrets import mask

    status = []
    for name, entry in PROVIDERS.items():
        value = provider_credential(name)
        status.append({
            "name": name,
            "label": entry["label"],
            "requires_key": entry["requires_key"],
            "requires_url": entry["requires_url"],
            "key_env": entry["key_env"],
            "signup": entry["signup"],
            "configured": bool(value),
            "credential": mask(value),
        })
    return status
