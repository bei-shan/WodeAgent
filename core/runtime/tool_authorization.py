"""Verified-identity capability fence for future hosted Agent workers.

Only trusted application wiring may supply an identity returned by OIDC
verification and a membership lookup backed by the authoritative store.
No model input, session snapshot, HTTP header or environment default can
construct an execution identity. This is defense-in-depth on ToolRegistry,
not a replacement for tenant-isolated sandbox mounts or service authorization.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from core.memory.contracts import MemoryScope


@dataclass(frozen=True, slots=True)
class HostedToolAuthority:
    """Immutable, server-bound project membership and tool allowlist.

    `check_membership` MUST query authoritative project membership on each
    invocation. Exceptions, timeouts and non-True return values deny access.
    The allowlist prevents a remotely loaded tool or Task subagent from silently
    gaining authority beyond the hosted execution policy.
    """

    scope: MemoryScope
    check_membership: Callable[[MemoryScope], bool] = field(repr=False, compare=False)
    allowed_tools: frozenset[str] = frozenset({"Bash"})

    def __post_init__(self) -> None:
        if not isinstance(self.scope, MemoryScope):
            raise TypeError("Hosted tool identity must be a verified MemoryScope")
        if not callable(self.check_membership):
            raise TypeError("Hosted project membership lookup is required")
        if (
            not isinstance(self.allowed_tools, frozenset)
            or not self.allowed_tools
            or any(not isinstance(n, str) or not n for n in self.allowed_tools)
        ):
            raise ValueError("Hosted tools require an explicit non-empty immutable allowlist")
        # Until each capability has resource-level scope enforcement, only
        # the isolated command adapter may be permitted by hosted workers.
        if self.allowed_tools != frozenset({"Bash"}):
            raise ValueError("Hosted tool execution currently supports only isolated Bash")

    def allows(self, name: str) -> bool:
        if name not in self.allowed_tools:
            return False
        try:
            # bool(1) is too permissive for a membership API: require explicit True.
            return self.check_membership(self.scope) is True
        except Exception:
            # Fail closed when the membership store is inaccessible.
            return False


def trusted_hosted_tool_authority(identity, store) -> HostedToolAuthority:
    """Trusted server adapter only; never expose this to model arguments.

    Call after OIDCVerifier.verify(). The identity object is not itself
    proof of membership; every tool check queries store.authorize_membership.
    """
    from desktop.service.hosted_identity import HostedIdentity

    if type(identity) is not HostedIdentity:
        raise TypeError("Only verified HostedIdentity can bind hosted tools")
    authorize_membership = getattr(store, "authorize_membership", None)
    if not callable(authorize_membership):
        raise TypeError("Authoritative membership store is required")
    return HostedToolAuthority(
        scope=identity.scope, check_membership=authorize_membership,
    )
