"""WebFetch response cache: TTL, size bound, bypass and failure handling."""
from __future__ import annotations

import pytest

from core.runtime import web_guard


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.setattr(web_guard, "resolve_host_ips", lambda host, port: {"93.184.216.34"})
    web_guard.clear_fetch_cache()
    yield
    web_guard.clear_fetch_cache()


def _result(url: str, text: str = "body") -> web_guard.FetchResult:
    return web_guard.FetchResult(
        url=url, final_url=url, status=200, content_type="text/html",
        text=text, bytes_read=len(text), redirects=0,
    )


@pytest.fixture
def calls(monkeypatch):
    recorded: list[dict] = []

    def fake(url, **kwargs):
        recorded.append({"url": url, **kwargs})
        return _result(url, f"body-{len(recorded)}")

    monkeypatch.setattr(web_guard, "_fetch_uncached", fake)
    return recorded


def test_second_fetch_of_same_url_is_served_from_cache(calls):
    first = web_guard.fetch_url("https://example.com/a")
    second = web_guard.fetch_url("https://example.com/a")
    assert len(calls) == 1
    assert second.text == first.text
    # Cached results are copies, so callers cannot mutate the cache.
    assert second is not first


def test_format_is_part_of_the_cache_key(calls):
    web_guard.fetch_url("https://example.com/a", format="markdown")
    web_guard.fetch_url("https://example.com/a", format="text")
    assert len(calls) == 2


def test_cache_can_be_bypassed(calls):
    web_guard.fetch_url("https://example.com/a")
    web_guard.fetch_url("https://example.com/a", use_cache=False)
    assert len(calls) == 2


def test_ttl_expiry_forces_a_refetch(calls, monkeypatch):
    monkeypatch.setattr(web_guard, "FETCH_CACHE_TTL_S", 0.0)
    web_guard.fetch_url("https://example.com/a")
    web_guard.fetch_url("https://example.com/a")
    assert len(calls) == 2


def test_failures_are_not_cached(monkeypatch, calls):
    def boom(url, **kwargs):
        raise web_guard.WebAccessError("nope")

    monkeypatch.setattr(web_guard, "_fetch_uncached", boom)
    for _ in range(2):
        with pytest.raises(web_guard.WebAccessError):
            web_guard.fetch_url("https://example.com/a")


def test_cache_is_bounded(calls):
    prior = web_guard.FETCH_CACHE_MAX_ENTRIES
    web_guard.FETCH_CACHE_MAX_ENTRIES = 3
    try:
        for index in range(6):
            web_guard.fetch_url(f"https://example.com/{index}")
        assert len(web_guard._FETCH_CACHE) == 3
    finally:
        web_guard.FETCH_CACHE_MAX_ENTRIES = prior


def test_clear_cache_forces_refetch(calls):
    web_guard.fetch_url("https://example.com/a")
    web_guard.clear_fetch_cache()
    web_guard.fetch_url("https://example.com/a")
    assert len(calls) == 2


def test_private_hosts_are_rejected_even_with_a_warm_cache(monkeypatch, calls):
    web_guard.fetch_url("https://example.com/a")
    monkeypatch.setattr(web_guard, "resolve_host_ips", lambda host, port: {"127.0.0.1"})
    with pytest.raises(web_guard.WebAccessDenied):
        web_guard.fetch_url("https://example.com/a")
