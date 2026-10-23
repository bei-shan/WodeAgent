"""Persisted context/compaction settings edited from the Web UI.

Environment variables still win at process start; this file is the operator
override that agents pick up when they are constructed, so a change in the UI
applies to new sessions without editing `.env` or restarting.
"""
from __future__ import annotations

import json
import os
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from core.secrets import data_dir


class ContextSettingsError(ValueError):
    """Rejected compaction setting."""


@dataclass
class ContextSettings:
    compression_threshold: float = 0.8
    min_retain_rounds: int = 10
    tool_prune_enabled: bool = True
    tool_prune_soft_ratio: float = 0.5
    tool_prune_keep_rounds: int = 3

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def settings_path() -> Path:
    return data_dir() / "settings" / "context.json"


def load_settings() -> ContextSettings:
    path = settings_path()
    if not path.is_file():
        return ContextSettings()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return ContextSettings()
    if not isinstance(raw, dict):
        return ContextSettings()
    return _validate(
        raw.get("compression_threshold", 0.8),
        raw.get("min_retain_rounds", 10),
        raw.get("tool_prune_enabled", True),
        raw.get("tool_prune_soft_ratio", 0.5),
        raw.get("tool_prune_keep_rounds", 3),
    )


def _validate(
    compression_threshold: Any,
    min_retain_rounds: Any,
    tool_prune_enabled: Any,
    tool_prune_soft_ratio: Any,
    tool_prune_keep_rounds: Any,
) -> ContextSettings:
    try:
        threshold = float(compression_threshold)
        retain = int(min_retain_rounds)
        prune_ratio = float(tool_prune_soft_ratio)
        prune_keep = int(tool_prune_keep_rounds)
    except (TypeError, ValueError) as exc:
        raise ContextSettingsError("Compaction settings must be numeric") from exc
    if not 0.5 <= threshold <= 0.95:
        raise ContextSettingsError("compression_threshold must be between 0.5 and 0.95")
    if not 2 <= retain <= 40:
        raise ContextSettingsError("min_retain_rounds must be between 2 and 40")
    if not 0.3 <= prune_ratio <= 0.9:
        raise ContextSettingsError("tool_prune_soft_ratio must be between 0.3 and 0.9")
    if not 1 <= prune_keep <= 10:
        raise ContextSettingsError("tool_prune_keep_rounds must be between 1 and 10")
    if prune_ratio >= threshold:
        raise ContextSettingsError("pruning must happen before summarization")
    return ContextSettings(
        compression_threshold=threshold,
        min_retain_rounds=retain,
        tool_prune_enabled=bool(tool_prune_enabled),
        tool_prune_soft_ratio=prune_ratio,
        tool_prune_keep_rounds=prune_keep,
    )


def save_settings(values: dict[str, Any]) -> ContextSettings:
    current = load_settings()
    merged = {**current.to_dict(), **{k: v for k, v in values.items() if v is not None}}
    checked = _validate(
        merged["compression_threshold"],
        merged["min_retain_rounds"],
        merged["tool_prune_enabled"],
        merged["tool_prune_soft_ratio"],
        merged["tool_prune_keep_rounds"],
    )
    path = settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        tmp.write_text(json.dumps(checked.to_dict(), indent=2), encoding="utf-8")
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)
    return checked


def read_overrides() -> dict[str, Any]:
    """Only the keys actually present in the file, fully validated."""
    path = settings_path()
    if not path.is_file():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(raw, dict):
        return {}
    values = load_settings().to_dict()
    return {key: values[key] for key in values if key in raw}


def apply_overrides(config: Any) -> None:
    """Apply file overrides onto a freshly built Config (env stays the base)."""
    for key, value in read_overrides().items():
        if hasattr(config, key):
            setattr(config, key, value)
