"""Persisted, local single-user memory settings.

`core.memory` is a backend-neutral foundation: it deliberately knows nothing
about files, endpoints or settings. This module owns the local policy — which
provider is enabled for this workspace and where its store lives — and writes
it atomically so the Web UI and agents read the same file.

Scope note: a MemoryScope must come from an authenticated principal. A local
single-user deployment has exactly one principal, so the scope is a constant
plus a project identifier derived from the workspace path.
"""
from __future__ import annotations

import hashlib
import json
import os
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .contracts import MemoryScope

SETTING_FILE = "settings/memory.json"
AVAILABLE_PROVIDERS = ("off", "sqlite")
DEFAULT_LIMIT = 8
MAX_LIMIT = 20


class MemorySettingsError(ValueError):
    """A rejected memory setting."""


@dataclass
class MemorySettings:
    enabled: bool = False
    provider: str = "off"
    limit: int = DEFAULT_LIMIT

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def data_dir(project_root: str | Path) -> Path:
    """Same root the Web service uses, so both surfaces share one store."""
    override = os.getenv("MYCODEAGENT_DATA_DIR", "").strip()
    if override:
        return Path(override)
    return Path(project_root) / ".mycodeagent"


def settings_path(project_root: str | Path) -> Path:
    return data_dir(project_root) / SETTING_FILE


def store_path(project_root: str | Path) -> Path:
    return data_dir(project_root) / "memory" / "memories.sqlite3"


def local_scope(project_root: str | Path) -> MemoryScope:
    """Constant local principal plus a stable, opaque project identifier."""
    digest = hashlib.sha256(str(Path(project_root).resolve()).encode("utf-8")).hexdigest()
    return MemoryScope(
        tenant_id="local",
        user_id="local",
        project_id="p" + digest[:15],
        agent_id="default",
    )


def load_settings(project_root: str | Path) -> MemorySettings:
    path = settings_path(project_root)
    if not path.is_file():
        return MemorySettings()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return MemorySettings()
    if not isinstance(raw, dict):
        return MemorySettings()
    provider = str(raw.get("provider") or "off")
    if provider not in AVAILABLE_PROVIDERS:
        provider = "off"
    try:
        limit = int(raw.get("limit") or DEFAULT_LIMIT)
    except (TypeError, ValueError):
        limit = DEFAULT_LIMIT
    return MemorySettings(
        enabled=bool(raw.get("enabled")),
        provider=provider,
        limit=max(1, min(MAX_LIMIT, limit)),
    )


def validate(enabled: bool, provider: str, limit: int) -> MemorySettings:
    if provider not in AVAILABLE_PROVIDERS:
        raise MemorySettingsError(f"Unsupported memory provider: {provider}")
    if enabled and provider == "off":
        raise MemorySettingsError("Choose a provider before enabling memory")
    if not 1 <= limit <= MAX_LIMIT:
        raise MemorySettingsError(f"limit must be 1..{MAX_LIMIT}")
    return MemorySettings(enabled=bool(enabled), provider=provider, limit=int(limit))


def save_settings(project_root: str | Path, settings: MemorySettings) -> MemorySettings:
    checked = validate(settings.enabled, settings.provider, settings.limit)
    path = settings_path(project_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        tmp.write_text(json.dumps(checked.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)
    return checked

def is_enabled(project_root: str | Path) -> bool:
    """True when the workspace has memory switched on with a real provider."""
    current = load_settings(project_root)
    return current.enabled and current.provider != "off"
