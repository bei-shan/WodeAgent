"""Fail-closed browser URL policy for the experimental local Agent Browser tool.

This is an APPLICATION-LAYER policy, not a kernel/network egress sandbox.
Only explicitly configured origins are accessible. Resolver checks reduce
SSRF risk but are not sufficient against DNS rebinding; never use for hosted
untrusted tenants without a network-layer proxy/enforced isolation.
"""
from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlsplit


class BrowserAccessDenied(PermissionError):
    pass


class BrowserURLPolicy:
    def __init__(self, allowed_origins: str, *, allow_loopback: bool = False):
        if not isinstance(allowed_origins, str) or len(allowed_origins) > 4096:
            raise ValueError("Browser allowed origins must be a bounded string")
        tokens = [t.strip() for t in allowed_origins.split(",")]
        if not tokens or len(tokens) > 20 or any(not t for t in tokens):
            raise ValueError("Browser requires 1-20 explicit comma-separated origins")
        origins = set()
        for raw in tokens:
            parts = urlsplit(raw)
            if (
                parts.scheme not in {"https", "http"} or not parts.hostname
                or parts.username is not None or parts.password is not None
                or parts.query or parts.fragment or parts.path not in {"", "/"}
                or "*" in raw or "\\" in raw or any(ord(c) < 32 for c in raw)
            ):
                raise ValueError("Browser origin must be an explicit http(s) origin")
            try:
                port = parts.port
            except ValueError as exc:
                raise ValueError("Invalid browser allowlist port") from exc
            host = parts.hostname.rstrip(".").lower()
            if not host or len(host) > 253 or "%" in host:
                raise ValueError("Invalid browser allowlist hostname")
            if parts.scheme == "http" and not (
                allow_loopback and self._literal_loopback(host)
            ):
                raise ValueError("Browser requires https except explicitly trusted loopback")
            origins.add((parts.scheme, host, port or (443 if parts.scheme == "https" else 80)))
        self._origins = frozenset(origins)
        self.allow_loopback = bool(allow_loopback)

    @staticmethod
    def _literal_loopback(host: str) -> bool:
        try:
            return ipaddress.ip_address(host).is_loopback
        except ValueError:
            return False

    def check(self, url: str) -> str:
        if not isinstance(url, str) or len(url) > 2048 or any(ord(c) < 32 for c in url):
            raise BrowserAccessDenied("Browser URL is invalid or oversized")
        try:
            parsed = urlsplit(url)
            port = parsed.port
        except ValueError as exc:
            raise BrowserAccessDenied("Malformed browser URL") from exc
        host = (parsed.hostname or "").rstrip(".").lower()
        if (
            parsed.scheme not in {"https", "http"} or not host
            or parsed.username is not None or parsed.password is not None
            or "\\" in url or "%" in host
        ):
            raise BrowserAccessDenied("Browser URL scheme or authority is forbidden")
        origin = (parsed.scheme, host, port or (443 if parsed.scheme == "https" else 80))
        if origin not in self._origins:
            raise BrowserAccessDenied("URL origin is outside operator allowlist")
        try:
            addresses = socket.getaddrinfo(host, origin[2], type=socket.SOCK_STREAM)
        except (OSError, OverflowError) as exc:
            raise BrowserAccessDenied("Browser hostname cannot be verified") from exc
        if not addresses:
            raise BrowserAccessDenied("Browser hostname has no addresses")
        for result in addresses:
            ip = ipaddress.ip_address(result[4][0].split("%", 1)[0])
            if ip.is_global:
                continue
            if self.allow_loopback and ip.is_loopback and self._literal_loopback(host):
                continue
            raise BrowserAccessDenied("Browser destination resolves to a non-public address")
        return url

    def allows(self, url: str) -> bool:
        try:
            self.check(url)
            return True
        except (BrowserAccessDenied, ValueError):
            return False
