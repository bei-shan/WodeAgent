"""Unified environment-variable helpers.

All config reads go through these functions so there is a single
place to add validation, logging, or migration logic.
"""

from __future__ import annotations

import os
from typing import Optional


def env_str(key: str, default: str = "") -> str:
    """Read a string environment variable, falling back to *default*."""
    value = os.getenv(key)
    if value is None:
        return default
    return value.strip()


def env_bool(key: str, default: bool = False) -> bool:
    """Read a boolean environment variable.

    Truthy values: ``1``, ``true``, ``yes``, ``y``, ``on`` (case-insensitive).
    Everything else (including unset) returns *default*.
    """
    value = os.getenv(key)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def env_int(key: str, default: int) -> int:
    """Read an integer environment variable, falling back to *default*."""
    value = os.getenv(key)
    if value is None:
        return default
    try:
        return int(value)
    except (ValueError, TypeError):
        return default


def env_float(key: str, default: float) -> float:
    """Read a float environment variable, falling back to *default*."""
    value = os.getenv(key)
    if value is None:
        return default
    try:
        return float(value)
    except (ValueError, TypeError):
        return default


def env_int_optional(key: str) -> Optional[int]:
    """Read an optional integer env var.  Returns ``None`` if unset or empty."""
    value = os.getenv(key)
    if value is None or value.strip() == "":
        return None
    try:
        return int(value)
    except (ValueError, TypeError):
        return None
