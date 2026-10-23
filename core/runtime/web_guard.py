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
import os
import re
import socket
import threading
import time
from dataclasses import dataclass, replace
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


BINARY_CONTENT_TYPES = frozenset(
    {
        "application/pdf",
        "application/zip",
        "application/gzip",
        "application/x-tar",
        "application/x-7z-compressed",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        "application/msword",
        "application/vnd.ms-excel",
        "application/octet-stream",
        "application/wasm",
        "font/woff",
        "font/woff2",
        "font/ttf",
        "font/otf",
    }
)


def _classify(content_type: str) -> str:
    mime = content_type.split(";", 1)[0].strip().lower()
    if mime in {"text/html", "application/xhtml+xml"} or not mime:
        return "html"
    if mime.startswith("text/") or mime in TEXT_CONTENT_TYPES:
        return "text"
    if (
        mime.startswith("image/")
        or mime.startswith("video/")
        or mime.startswith("audio/")
        or mime in BINARY_CONTENT_TYPES
    ):
        return "binary"
    return "unknown"


# ---------------------------------------------------------------------------
# robots.txt (opt-in; off by default because a local agent acts for its user)
# ---------------------------------------------------------------------------
ROBOTS_CACHE_TTL_S = 3600.0
_ROBOTS_CACHE: "dict[str, tuple[Optional[RobotsRules], float]]" = {}
_ROBOTS_CACHE_LOCK = threading.Lock()


def robots_enabled() -> bool:
    value = (os.getenv("WEB_FETCH_RESPECT_ROBOTS", "false") or "").strip().lower()
    return value in {"1", "true", "yes", "on"}


def _parse_robots_groups(text: str) -> list[tuple[list[str], list[tuple[bool, str]]]]:
    groups: list[tuple[list[str], list[tuple[bool, str]]]] = []
    agents: list[str] = []
    rules: list[tuple[bool, str]] = []
    in_rules = False
    for raw in (text or "").splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line or ":" not in line:
            continue
        field, _, value = line.partition(":")
        field = field.strip().lower()
        value = value.strip()
        if field == "user-agent":
            if in_rules:
                groups.append((agents, rules))
                agents, rules, in_rules = [], [], False
            agents.append(value.lower())
        elif field in {"allow", "disallow"} and agents:
            in_rules = True
            rules.append((field == "allow", value))
    if agents:
        groups.append((agents, rules))
    return groups


def _robots_match(pattern: str, path: str) -> bool:
    if not pattern:
        return False
    anchored = pattern.endswith("$")
    body = pattern[:-1] if anchored else pattern
    regex = re.escape(body).replace(r"\*", ".*")
    try:
        if anchored:
            return re.fullmatch(regex, path) is not None
        return re.match(regex, path) is not None
    except re.error:
        return False


class RobotsRules:
    """Minimal RFC 9309 matcher: agent groups, prefixes, wildcards and anchors."""

    def __init__(self, text: str = "", user_agent: str = DEFAULT_USER_AGENT):
        self._rules = self._select(_parse_robots_groups(text), user_agent)

    @staticmethod
    def _select(groups, user_agent: str) -> list[tuple[bool, str]]:
        ua = (user_agent or "").lower()
        specific: list[tuple[bool, str]] = []
        wildcard: list[tuple[bool, str]] = []
        for agents, rules in groups:
            named = [agent for agent in agents if agent and agent != "*"]
            if any(agent in ua for agent in named):
                specific.extend(rules)
            elif "*" in agents:
                wildcard.extend(rules)
        return specific if specific else wildcard

    def allows(self, path: str) -> bool:
        best_length = -1
        decision = True
        for allow, pattern in self._rules:
            if not _robots_match(pattern, path):
                continue
            length = len(pattern.rstrip("$"))
            if length > best_length or (length == best_length and allow):
                best_length = length
                decision = allow
        return decision


def _fetch_robots(origin: str, timeout_s: float) -> Optional[str]:
    import httpx

    try:
        with httpx.Client(
            timeout=timeout_s, follow_redirects=True,
            headers={"User-Agent": DEFAULT_USER_AGENT},
        ) as client:
            response = client.get(f"{origin}/robots.txt")
    except httpx.HTTPError:
        return None
    if response.status_code >= 400:
        return None
    return response.text


def _robots_for(
    origin: str, *, user_agent: str, timeout_s: float, fetcher=None,
) -> Optional[RobotsRules]:
    now = time.monotonic()
    with _ROBOTS_CACHE_LOCK:
        cached = _ROBOTS_CACHE.get(origin)
        if cached is not None and cached[1] > now:
            return cached[0]
    text = (fetcher or _fetch_robots)(origin, timeout_s)
    rules = RobotsRules(text, user_agent) if text else None
    with _ROBOTS_CACHE_LOCK:
        _ROBOTS_CACHE[origin] = (rules, now + ROBOTS_CACHE_TTL_S)
    return rules


def clear_robots_cache() -> None:
    with _ROBOTS_CACHE_LOCK:
        _ROBOTS_CACHE.clear()


def robots_allows(
    url: str, *, user_agent: str = DEFAULT_USER_AGENT, timeout_s: float = 5.0, fetcher=None,
) -> bool:
    parts = urlsplit(url)
    origin = f"{parts.scheme}://{parts.hostname or ''}"
    if parts.port:
        origin = f"{origin}:{parts.port}"
    rules = _robots_for(origin, user_agent=user_agent, timeout_s=timeout_s, fetcher=fetcher)
    return True if rules is None else rules.allows(parts.path or "/")


FETCH_CACHE_TTL_S = 300.0
FETCH_CACHE_MAX_ENTRIES = 32
_FETCH_CACHE: "dict[tuple[str, str], tuple[FetchResult, float]]" = {}
_FETCH_CACHE_LOCK = threading.Lock()


def clear_fetch_cache() -> None:
    with _FETCH_CACHE_LOCK:
        _FETCH_CACHE.clear()


def _cache_get(key: tuple[str, str], now: float) -> Optional[FetchResult]:
    with _FETCH_CACHE_LOCK:
        entry = _FETCH_CACHE.get(key)
        if entry is None:
            return None
        result, expires_at = entry
        if expires_at <= now:
            _FETCH_CACHE.pop(key, None)
            return None
        return replace(result)


def _cache_put(key: tuple[str, str], result: FetchResult, now: float) -> None:
    with _FETCH_CACHE_LOCK:
        if len(_FETCH_CACHE) >= FETCH_CACHE_MAX_ENTRIES:
            oldest = min(_FETCH_CACHE, key=lambda item: _FETCH_CACHE[item][1])
            _FETCH_CACHE.pop(oldest, None)
        _FETCH_CACHE[key] = (replace(result), now + FETCH_CACHE_TTL_S)


def fetch_url(
    url: str,
    *,
    timeout_s: float = DEFAULT_TIMEOUT_S,
    max_bytes: int = DEFAULT_MAX_BYTES,
    max_redirects: int = DEFAULT_MAX_REDIRECTS,
    user_agent: str = DEFAULT_USER_AGENT,
    rate_limiter: Optional[HostRateLimiter] = None,
    format: str = "markdown",
    use_cache: bool = True,
) -> FetchResult:
    """Fetch one public URL, following redirects with re-validation.

    ``format`` selects how HTML becomes model-readable text: ``markdown`` keeps
    headings, links, lists and code fences; ``text`` flattens to prose. Successful
    responses are cached for ``FETCH_CACHE_TTL_S`` so repeated reads of the same
    page neither re-download nor re-consume the per-host budget.
    """
    normalized = assert_public_url(url)
    key = (normalized, format)
    now = time.monotonic()
    if use_cache:
        cached = _cache_get(key, now)
        if cached is not None:
            return cached
    result = _fetch_uncached(
        normalized,
        timeout_s=timeout_s,
        max_bytes=max_bytes,
        max_redirects=max_redirects,
        user_agent=user_agent,
        rate_limiter=rate_limiter,
        format=format,
    )
    if use_cache:
        _cache_put(key, result, now)
    return result


def _fetch_uncached(
    url: str,
    *,
    timeout_s: float = DEFAULT_TIMEOUT_S,
    max_bytes: int = DEFAULT_MAX_BYTES,
    max_redirects: int = DEFAULT_MAX_REDIRECTS,
    user_agent: str = DEFAULT_USER_AGENT,
    rate_limiter: Optional[HostRateLimiter] = None,
    format: str = "markdown",
) -> FetchResult:
    """Perform the request; *url* must already be normalized and validated."""
    import httpx

    if robots_enabled() and not robots_allows(
        url, user_agent=user_agent, timeout_s=min(5.0, timeout_s),
    ):
        raise WebAccessDenied(f"robots.txt disallows fetching {url}")
    as_markdown = format != "text"
    limiter = rate_limiter or DEFAULT_RATE_LIMITER
    current = url
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
