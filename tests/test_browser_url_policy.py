"""Browser URL policy is a restricted origin capability, never model-controlled."""
from __future__ import annotations

import socket
import pytest

from core.browser_url_policy import BrowserAccessDenied, BrowserURLPolicy


def test_requires_explicit_origin_and_https():
    for value in ("", "https://", "https://a.example/path", "https://*.example",
                  "https://user:pass@a.example", "file:///etc/passwd",
                  "http://example.com", "https://a.example?x=1",
                  "https://a.example,"):
        with pytest.raises(ValueError):
            BrowserURLPolicy(value)


def test_enforces_exact_scheme_host_and_port(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: [
        (socket.AF_INET, socket.SOCK_STREAM, 0, "", ("93.184.215.14", 443))
    ])
    policy = BrowserURLPolicy("https://example.com")
    for url in ("https://example.com.evil.invalid/", "https://other.example/",
                "https://example.com:8443/", "http://example.com/",
                "https://user@example.com/", "file:///etc/passwd",
                "https://example.com\\@evil.invalid/"):
        with pytest.raises(BrowserAccessDenied):
            policy.check(url)
    assert policy.check("https://example.com/allowed") == "https://example.com/allowed"


def test_rejects_private_dns_answers_and_mixed_public_private(monkeypatch):
    policy = BrowserURLPolicy("https://a.example")
    for ipset in [("127.0.0.1",), ("10.0.0.3",), ("169.254.169.254",),
                  ("93.184.215.14", "192.168.1.1")]:
        monkeypatch.setattr(socket, "getaddrinfo", lambda *a, ipset=ipset, **k: [
            (socket.AF_INET, socket.SOCK_STREAM, 0, "", (ip, 443)) for ip in ipset
        ])
        with pytest.raises(BrowserAccessDenied):
            policy.check("https://a.example/")


def test_loopback_needs_explicit_operator_opt_in():
    with pytest.raises(ValueError):
        BrowserURLPolicy("http://127.0.0.1:8179")
    policy = BrowserURLPolicy("http://127.0.0.1:8179", allow_loopback=True)
    assert policy.check("http://127.0.0.1:8179/") == "http://127.0.0.1:8179/"
    with pytest.raises(BrowserAccessDenied):
        policy.check("http://127.0.0.1:8180/")
    with pytest.raises(BrowserAccessDenied):
        BrowserURLPolicy("https://example.com", allow_loopback=True).check(
            "https://127.0.0.1/")


def test_unknown_dns_fails_closed(monkeypatch):
    policy = BrowserURLPolicy("https://a.example")
    def fail(*a, **k):
        raise socket.gaierror("not found")
    monkeypatch.setattr(socket, "getaddrinfo", fail)
    assert policy.allows("https://a.example/") is False
