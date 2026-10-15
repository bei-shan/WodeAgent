"""Opt-in, explicit-scope long-term memory foundation.

This package does not modify the existing history, context, or Web endpoints.
An authenticated, persisted settings path and provider adapters are required
before exposing multi-user memory as a production feature.
"""

from .contracts import MemoryBackend, MemoryRecord, MemoryScope
from .registry import MemoryProviderRegistry, MemoryProviderUnavailable
from .sqlite_backend import SQLiteMemoryBackend
from .mem0_backend import Mem0MemoryBackend

__all__ = [
    "MemoryBackend",
    "MemoryRecord",
    "MemoryScope",
    "MemoryProviderRegistry",
    "MemoryProviderUnavailable",
    "SQLiteMemoryBackend",
    "Mem0MemoryBackend",
]
