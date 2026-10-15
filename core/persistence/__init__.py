"""Optional, explicitly scoped persistence adapters.

The local API still uses SessionManager until verified multi-user identity,
authorization and endpoint ownership checks are integrated.
"""

from .postgres_sessions import (
    PostgresPrivateSessionStore,
    StoredPrivateSession,
    SessionVersionConflict,
)

__all__ = [
    "PostgresPrivateSessionStore",
    "StoredPrivateSession",
    "SessionVersionConflict",
]
