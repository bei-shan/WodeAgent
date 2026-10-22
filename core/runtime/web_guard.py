"""Network egress guard shared by model-facing web tools.

The shape follows hardened implementations in mature agent CLIs (Gemini CLI
``web-fetch``, OpenHands, Codex): an http(s) allowlist, DNS-resolved denial of
private/loopback/link-local targets, redirect re-validation on every hop,
per-host rate limiting, hard byte caps, and HTML-to-text extraction before any
content reaches model context.

This module performs *policy*, not authorization. It cannot replace sandbox
isolation or operator consent for tools that execute effects.
"""
from __future__ import annotations

import ipaddress
import re
import socket
import threading
import time
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Optional
from urllib.parse import urljoin, urlsplit, urlunsplit

from core.runtime.html_to_markdown import html_to_markdown

ALLOWED_SCHEMES = frozenset({"http", "https"})
BLOCKED_HOSTS = frozenset({"localhost", "localhost.localdomain", "ip6-localhost", "ip6-loopback"})
BLOCKED_HOST_SUFFIXES = (".localhost", ".local", ".internal", ".home.arpa", ".lan")

DEFAULT_TIMEOUT_S = 20.0
DEFAULT_MAX_BYTES = 5 * 1024 * 1024
DEFAULT_MAX_REDIRECTS = 5
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (compatible; RidgeCode/1.0; +https://github.com/bei-shan/WodeAgent)"
)
REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
TEXT_CONTENT_TYPES = frozenset(
    {
        "application/json",
        "application/xml",
        "application/javascript",
        "application/x-yaml",
        "application/yaml",
        "application/rss+xml",
        "application/atom+xml",
    }
)


class WebAccessError(Exception):
    """Raised when a network request cannot be completed as requested."""


class WebAccessDenied(WebAccessError):
    """Raised when policy refuses the target (scheme, host or rate limit)."""


class WebAccessTimeout(WebAccessError):
    """Raised when the remote host does not answer inside the budget."""


# ---------------------------------------------------------------------------
# URL validation
# ---------------------------------------------------------------------------
def normalize_url(raw: str) -> str:
    """Return a canonical http(s) URL or raise :class:`WebAccessDenied`."""
    if not isinstance(raw, str):
        raise WebAccessDenied("URL must be a string")
    candidate = raw.strip()
    if not candidate:
        raise WebAccessDenied("URL is empty")
    if len(candidate) > 4096:
        raise WebAccessDenied("URL is too long")
    try:
        parts = urlsplit(candidate)
    except ValueError as exc:
        raise WebAccessDenied(f"Malformed URL: {exc}") from exc
    scheme = parts.scheme.lower()
    if scheme not in ALLOWED_SCHEMES:
        raise WebAccessDenied(f"Unsupported URL scheme {scheme!r}; only http and https are allowed")
    if not parts.hostname:
        raise WebAccessDenied("URL has no hostname")
    if parts.username or parts.password:
        raise WebAccessDenied("URL must not embed credentials")
    host = parts.hostname.lower()
    if host.endswith("."):
        host = host.rstrip(".")
    netloc = host
    if parts.port is not None:
        netloc = f"{host}:{parts.port}"
    path = parts.path or "/"
    return urlunsplit((scheme, netloc, path, parts.query, ""))


def _port_for(parts) -> int:
    if parts.port is not None:
        return parts.port
    return 443 if parts.scheme.lower() == "https" else 80


def is_blocked_ip(address: str) -> bool:
    """True when *address* is not a publicly routable unicast target."""
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return True
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return bool(
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    )


def resolve_host_ips(host: str, port: int) -> set[str]:
    """Resolve every address a hostname points at, or raise."""
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise WebAccessError(f"Cannot resolve host {host!r}: {exc}") from exc
    return {info[4][0] for info in infos}


def assert_public_url(raw: str) -> str:
    """Validate a URL and prove its resolved addresses are public.

    Resolution happens before the request so a hostname cannot smuggle a
    loopback or metadata address past the policy.
    """
    url = normalize_url(raw)
    parts = urlsplit(url)
    host = parts.hostname or ""
    if host in BLOCKED_HOSTS or any(host.endswith(suffix) for suffix in BLOCKED_HOST_SUFFIXES):
        raise WebAccessDenied(f"Host {host!r} is not reachable from web tools")
    for address in resolve_host_ips(host, _port_for(parts)):
        if is_blocked_ip(address):
            raise WebAccessDenied(
                f"Host {host!r} resolves to non-public address {address}; refused"
            )
    return url


# ---------------------------------------------------------------------------
# Rate limiting
# ---------------------------------------------------------------------------
class HostRateLimiter:
    """Fixed-window per-host limiter; the default mirrors Gemini CLI's 10/min."""

    def __init__(self, max_requests: int = 10, window_s: float = 60.0, clock=time.monotonic):
        if max_requests < 1:
            raise ValueError("max_requests must be positive")
        self.max_requests = max_requests
        self.window_s = window_s
        self._clock = clock
        self._lock = threading.Lock()
        self._history: dict[str, list[float]] = {}

    def acquire(self, host: str) -> None:
        now = self._clock()
        cutoff = now - self.window_s
        with self._lock:
            stamps = [stamp for stamp in self._history.get(host, []) if stamp > cutoff]
            if len(stamps) >= self.max_requests:
                self._history[host] = stamps
                wait = max(0.0, stamps[0] + self.window_s - now)
                raise WebAccessDenied(
                    f"Rate limit reached for {host!r}; retry in {wait:.0f}s"
                )
            stamps.append(now)
            self._history[host] = stamps


DEFAULT_RATE_LIMITER = HostRateLimiter()


# ---------------------------------------------------------------------------
# HTML to text
# ---------------------------------------------------------------------------
class _TextExtractor(HTMLParser):
    _SKIP = frozenset(
        {"script", "style", "noscript", "svg", "template", "iframe", "head", "canvas"}
    )
    _BLOCK = frozenset(
        {
            "p", "div", "br", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "section",
            "article", "header", "footer", "pre", "blockquote", "table", "ul", "ol",
            "nav", "main", "aside", "figure", "figcaption", "hr", "form", "fieldset",
        }
    )
    _CELL = frozenset({"td", "th"})

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._title_parts: list[str] = []
        self._skip_depth = 0
        self._in_title = False

    def handle_starttag(self, tag, attrs):
        if tag in self._SKIP:
            self._skip_depth += 1
        elif tag == "title":
            self._in_title = True
        elif tag == "li":
            self.parts.append("\n- ")
        elif tag in self._CELL:
            self.parts.append(" | ")
        elif tag in self._BLOCK:
            self.parts.append("\n")

    def handle_startendtag(self, tag, attrs):
        if tag in self._BLOCK:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in self._SKIP:
            self._skip_depth = max(0, self._skip_depth - 1)
        elif tag == "title":
            self._in_title = False
        elif tag in self._BLOCK:
            self.parts.append("\n")

    def handle_data(self, data):
        if self._in_title:
            self._title_parts.append(data)
            return
        if self._skip_depth:
            return
        self.parts.append(data)


def html_to_text(html: str) -> str:
    """Convert an HTML document to readable plain text."""
    parser = _TextExtractor()
    try:
        parser.feed(html or "")
        parser.close()
    except Exception:  # malformed markup must still yield what was read
        pass
    raw = "".join(parser.parts).replace("\xa0", " ")
    raw = re.sub(r"[ \t\f\v\r]+", " ", raw)
    raw = re.sub(r" *\n *", "\n", raw)
    raw = re.sub(r"\n{3,}", "\n\n", raw).strip()
    title = " ".join("".join(parser._title_parts).split())
    if title and not raw.startswith(title):
        return f"{title}\n\n{raw}" if raw else title
    return raw


# ---------------------------------------------------------------------------
# Fetching
# ---------------------------------------------------------------------------
@dataclass
class FetchResult:
    url: str
    final_url: str
    status: int
    content_type: str
    text: str
    bytes_read: int
    redirects: int
    truncated_bytes: bool = False


def _charset_of(content_type: str) -> str:
    match = re.search(r"charset=([\w\-]+)", content_type, re.IGNORECASE)
    return match.group(1) if match else "utf-8"


def _decode(body: bytes, content_type: str) -> str:
    return body.decode(_charset_of(content_type), errors="replace")


def _classify(content_type: str) -> str:
    mime = content_type.split(";", 1)[0].strip().lower()
    if mime in {"text/html", "application/xhtml+xml"} or not mime:
        return "html"
    if mime.startswith("text/") or mime in TEXT_CONTENT_TYPES:
        return "text"
    if mime.startswith("image/") or mime.startswith("video/") or mime.startswith("audio/"):
        return "binary"
    return "unknown"


def fetch_url(
    url: str,
    *,
    timeout_s: float = DEFAULT_TIMEOUT_S,
    max_bytes: int = DEFAULT_MAX_BYTES,
    max_redirects: int = DEFAULT_MAX_REDIRECTS,
    user_agent: str = DEFAULT_USER_AGENT,
    rate_limiter: Optional[HostRateLimiter] = None,
    format: str = "markdown",
) -> FetchResult:
    """Fetch one public URL, following redirects with re-validation.

    ``format`` selects how HTML becomes model-readable text: ``markdown`` keeps
    headings, links, lists and code fences; ``text`` flattens to prose.
    """
    import httpx

    as_markdown = format != "text"
    limiter = rate_limiter or DEFAULT_RATE_LIMITER
    current = assert_public_url(url)
    headers = {
        "User-Agent": user_agent,
        "Accept": "text/html,application/xhtml+xml,text/plain;q=0.9,application/json;q=0.8,*/*;q=0.5",
    }
    redirects = 0
    try:
        with httpx.Client(timeout=timeout_s, headers=headers, follow_redirects=False) as client:
            while True:
                parts = urlsplit(current)
                limiter.acquire(parts.hostname or "")
                with client.stream("GET", current) as response:
                    status = response.status_code
                    if status in REDIRECT_STATUSES:
                        location = response.headers.get("location")
                        if not location:
                            raise WebAccessError(f"HTTP {status} redirect without a Location header")
                        if redirects >= max_redirects:
                            raise WebAccessError(f"Exceeded {max_redirects} redirects")
                        redirects += 1
                        current = assert_public_url(urljoin(current, location))
                        continue

                    content_type = response.headers.get("content-type", "")
                    body = bytearray()
                    truncated = False
                    for chunk in response.iter_bytes():
                        body.extend(chunk)
                        if len(body) > max_bytes:
                            truncated = True
                            break
                    if status >= 400:
                        snippet = _decode(bytes(body[:400]), content_type)
                        raise WebAccessError(f"HTTP {status} from {current}: {snippet.strip()[:300]}")
                    kind = _classify(content_type)
                    if kind == "binary":
                        raise WebAccessError(
                            f"Unsupported content type {content_type or 'unknown'!r}; WebFetch returns text only"
                        )
                    raw = _decode(bytes(body), content_type)
                    if kind == "html":
                        text = html_to_markdown(raw, current) if as_markdown else html_to_text(raw)
                        if as_markdown and len(text.strip()) < 80:
                            # JS-heavy or consent-wall pages extract to nothing;
                            # fall back to the plain-text rendering before failing.
                            fallback = html_to_text(raw).strip()
                            if len(fallback) > len(text.strip()):
                                text = fallback
                        if len(text.strip()) < 20:
                            raise WebAccessError(
                                "No readable text extracted from this page "
                                "(it is probably JavaScript-rendered)."
                            )
                    else:
                        text = raw
                    return FetchResult(
                        url=url,
                        final_url=current,
                        status=status,
                        content_type=content_type.split(";", 1)[0].strip() or "unknown",
                        text=text,
                        bytes_read=len(body),
                        redirects=redirects,
                        truncated_bytes=truncated,
                    )
    except httpx.TimeoutException as exc:
        raise WebAccessTimeout(f"Timed out after {timeout_s:g}s fetching {current}") from exc
    except httpx.HTTPError as exc:
        raise WebAccessError(f"Network error fetching {current}: {exc}") from exc
