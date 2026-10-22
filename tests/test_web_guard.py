"""Network egress policy for model-facing web tools."""
from __future__ import annotations

import pytest

from core.runtime import web_guard


@pytest.fixture(autouse=True)
def _public_dns(monkeypatch):
    """Keep policy tests hermetic: no real DNS lookups."""
    monkeypatch.setattr(web_guard, "resolve_host_ips", lambda host, port: {"93.184.216.34"})


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "ftp://example.com/x",
        "javascript:alert(1)",
        "mailto:a@b.c",
        "example.com/no-scheme",
    ],
)
def test_rejects_non_http_schemes(url):
    with pytest.raises(web_guard.WebAccessDenied):
        web_guard.assert_public_url(url)


def test_rejects_embedded_credentials():
    with pytest.raises(web_guard.WebAccessDenied):
        web_guard.assert_public_url("https://user:secret@example.com/")


@pytest.mark.parametrize(
    "host",
    ["localhost", "app.localhost", "printer.local", "metadata.internal", "box.lan"],
)
def test_rejects_internal_hostnames(host):
    with pytest.raises(web_guard.WebAccessDenied):
        web_guard.assert_public_url(f"http://{host}/status")


@pytest.mark.parametrize(
    "address",
    ["127.0.0.1", "10.1.2.3", "192.168.1.10", "172.16.5.4", "169.254.169.254", "0.0.0.0", "::1"],
)
def test_rejects_non_public_resolved_addresses(monkeypatch, address):
    monkeypatch.setattr(web_guard, "resolve_host_ips", lambda host, port: {address})
    with pytest.raises(web_guard.WebAccessDenied):
        web_guard.assert_public_url("https://example.com/")


def test_rejects_when_any_resolved_address_is_private(monkeypatch):
    monkeypatch.setattr(
        web_guard, "resolve_host_ips", lambda host, port: {"93.184.216.34", "127.0.0.1"}
    )
    with pytest.raises(web_guard.WebAccessDenied):
        web_guard.assert_public_url("https://example.com/")


def test_accepts_public_host_and_normalizes():
    assert (
        web_guard.normalize_url("HTTPS://Example.COM:443/a/b#frag")
        == "https://example.com:443/a/b"
    )
    assert web_guard.assert_public_url("https://example.com/") == "https://example.com/"


def test_is_blocked_ip_classification():
    assert web_guard.is_blocked_ip("127.0.0.1")
    assert web_guard.is_blocked_ip("::1")
    assert web_guard.is_blocked_ip("169.254.169.254")
    assert web_guard.is_blocked_ip("not-an-ip")
    assert not web_guard.is_blocked_ip("93.184.216.34")


def test_rate_limiter_blocks_after_window_budget():
    now = [1000.0]
    limiter = web_guard.HostRateLimiter(max_requests=2, window_s=60, clock=lambda: now[0])
    limiter.acquire("example.com")
    limiter.acquire("example.com")
    with pytest.raises(web_guard.WebAccessDenied):
        limiter.acquire("example.com")
    now[0] += 61
    limiter.acquire("example.com")  # window rolled over


def test_rate_limiter_is_per_host():
    limiter = web_guard.HostRateLimiter(max_requests=1, window_s=60)
    limiter.acquire("a.example")
    limiter.acquire("b.example")


def test_html_to_text_drops_scripts_and_keeps_structure():
    html = """
    <html><head><title>Demo page</title><style>.x{color:red}</style></head>
    <body>
      <h1>Heading</h1>
      <script>alert('x')</script>
      <p>First &amp; second sentence.</p>
      <ul><li>alpha</li><li>beta</li></ul>
    </body></html>
    """
    text = web_guard.html_to_text(html)
    assert text.startswith("Demo page")
    assert "Heading" in text
    assert "First & second sentence." in text
    assert "- alpha" in text
    assert "alert(" not in text
    assert "color:red" not in text
    assert "\n\n\n" not in text
