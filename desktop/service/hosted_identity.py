"""Strict OIDC ID/access token verification for a *separate* hosted API.

The issuer must be configured to sign trusted, validated tenant and project
membership claims. A token from an arbitrary OIDC issuer is not sufficient
proof of organization membership: provision tenants through the issuer and
enforce its claim contract before exposing shared resources.
"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass
from urllib.parse import urlparse

from core.memory.contracts import MemoryScope


class InvalidPrincipal(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class HostedIdentity:
    scope: MemoryScope


@dataclass(frozen=True, slots=True)
class OIDCSettings:
    issuer: str
    audience: str
    jwks_url: str
    tenant_claim: str = "ridge_tenant"
    project_claim: str = "ridge_project"
    max_token_age_seconds: int = 3600

    @classmethod
    def from_env(cls) -> "OIDCSettings":
        required = ("RIDGE_OIDC_ISSUER", "RIDGE_OIDC_AUDIENCE", "RIDGE_OIDC_JWKS_URL")
        vals = [os.environ.get(v, "").strip() for v in required]
        if any(not v for v in vals):
            raise RuntimeError("Hosted service requires verified OIDC issuer, audience and JWKS URL")
        age = int(os.getenv("RIDGE_OIDC_MAX_TOKEN_AGE_SECONDS", "3600"))
        return cls(
            issuer=vals[0], audience=vals[1], jwks_url=vals[2],
            tenant_claim=os.getenv("RIDGE_OIDC_TENANT_CLAIM", "ridge_tenant"),
            project_claim=os.getenv("RIDGE_OIDC_PROJECT_CLAIM", "ridge_project"),
            max_token_age_seconds=age,
        )

    def __post_init__(self) -> None:
        # HTTPS required; no URL fragments or embedded credentials. An
        # administrator must explicitly set this endpoint, not a token issuer.
        parsed = urlparse(self.jwks_url)
        if (parsed.scheme != "https" or not parsed.hostname or parsed.username
                or parsed.password or parsed.fragment):
            raise ValueError("OIDC JWKS URL must be an explicit HTTPS URL")
        issuer = urlparse(self.issuer)
        if issuer.scheme != "https" or not issuer.hostname or issuer.fragment:
            raise ValueError("OIDC issuer must be HTTPS")
        if not self.audience.strip() or not 60 <= self.max_token_age_seconds <= 86400:
            raise ValueError("Invalid OIDC audience or token age")
        for claim in (self.tenant_claim, self.project_claim):
            if not claim or len(claim) > 128 or claim.startswith("_"):
                raise ValueError("Invalid OIDC identity claim name")


class OIDCVerifier:
    """PyJWT/JWKS verifier, safe for asyncio.to_thread, never trusts raw claims."""

    def __init__(self, settings: OIDCSettings):
        self.settings = settings
        try:
            from jwt import PyJWKClient
        except ImportError as exc:
            raise RuntimeError("Install requirements-hosted.txt for hosted OIDC") from exc
        self._jwks = PyJWKClient(
            settings.jwks_url, cache_jwk_set=True, lifespan=300, timeout=5,
        )

    def verify(self, bearer_token: str) -> HostedIdentity:
        try:
            import jwt

            if not isinstance(bearer_token, str) or not 0 < len(bearer_token) <= 8192:
                raise InvalidPrincipal("Invalid bearer token")
            signing_key = self._jwks.get_signing_key_from_jwt(bearer_token)
            claims = jwt.decode(
                bearer_token,
                signing_key.key,
                algorithms=["RS256", "ES256"],
                audience=self.settings.audience,
                issuer=self.settings.issuer,
                options={"require": ["exp", "iat", "iss", "aud", "sub"]},
                leeway=30,
            )
            now = time.time()
            issued = claims["iat"]
            if (type(issued) not in (int, float)
                    or issued > now + 30
                    or now - issued > self.settings.max_token_age_seconds):
                raise InvalidPrincipal("OIDC token is too old")
            if not isinstance(claims["sub"], str):
                raise InvalidPrincipal("Invalid OIDC subject")
            # These are SIGNED trusted claims provisioned by the issuer. No
            # user-controlled headers, query parameters or body fields accepted.
            scope = MemoryScope(
                tenant_id=claims.get(self.settings.tenant_claim),
                project_id=claims.get(self.settings.project_claim),
                user_id=claims["sub"],
            )
            return HostedIdentity(scope=scope)
        except Exception as exc:
            if isinstance(exc, InvalidPrincipal):
                raise
            raise InvalidPrincipal("Invalid or expired OIDC token") from exc
