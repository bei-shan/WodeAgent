"""robots.txt parsing/matching (opt-in) and binary content rejection."""
from __future__ import annotations

import pytest

from core.runtime import web_guard


@pytest.fixture(autouse=True)
def _clean():
    web_guard.clear_robots_cache()
    yield
    web_guard.clear_robots_cache()


def _rules(text: str, ua: str = web_guard.DEFAULT_USER_AGENT) -> web_guard.RobotsRules:
    return web_guard.RobotsRules(text, ua)


def test_disallow_prefix_blocks_only_that_subtree():
    rules = _rules("User-agent: *\nDisallow: /private\nAllow: /private/ok\n")
    assert rules.allows("/public/page")
    assert not rules.allows("/private/data")
    assert rules.allows("/private/ok")


def test_longest_match_wins_and_allow_wins_ties():
    rules = _rules("User-agent: *\nDisallow: /a\nAllow: /a/b\n")
    assert not rules.allows("/a/x")
    assert rules.allows("/a/b/c")


def test_wildcards_and_end_anchor():
    rules = _rules("User-agent: *\nDisallow: /*.pdf$\nDisallow: /tmp/*/secret\n")
    assert not rules.allows("/docs/report.pdf")
    assert rules.allows("/docs/report.pdf.html")
    assert not rules.allows("/tmp/abc/secret")
    assert rules.allows("/tmp/abc/public")


def test_specific_agent_group_wins_over_wildcard():
    text = (
        "User-agent: *\nDisallow: /\n\n"
        "User-agent: RidgeCode\nDisallow: /blocked\n"
    )
    ridge = _rules(text)
    assert ridge.allows("/anything")
    assert not ridge.allows("/blocked/page")
    other = _rules(text, "SomeOtherBot/1.0")
    assert not other.allows("/anything")


def test_empty_or_malformed_robots_allows_everything():
    assert _rules("").allows("/x")
    assert _rules("garbage without fields").allows("/x")
    assert _rules("User-agent: *\nDisallow:\n").allows("/x")


def test_robots_allows_uses_the_fetcher_and_caches(monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(web_guard, "robots_enabled", lambda: True)

    def fetcher(origin: str, timeout_s: float):
        calls.append(origin)
        return "User-agent: *\nDisallow: /no\n"

    assert web_guard.robots_allows("https://example.com/yes", fetcher=fetcher) is True
    assert web_guard.robots_allows("https://example.com/no", fetcher=fetcher) is False
    assert calls == ["https://example.com"]  # one fetch, cached afterwards


def test_robots_allows_fails_open_when_robots_is_unavailable():
    assert web_guard.robots_allows(
        "https://example.com/anything", fetcher=lambda origin, timeout_s: None,
    ) is True


def test_robots_check_is_skipped_unless_enabled(monkeypatch):
    monkeypatch.delenv("WEB_FETCH_RESPECT_ROBOTS", raising=False)
    assert web_guard.robots_enabled() is False
    monkeypatch.setenv("WEB_FETCH_RESPECT_ROBOTS", "true")
    assert web_guard.robots_enabled() is True


@pytest.mark.parametrize(
    "content_type,expected",
    [
        ("text/html; charset=utf-8", "html"),
        ("application/json", "text"),
        ("application/pdf", "binary"),
        ("application/zip", "binary"),
        ("image/png", "binary"),
        ("application/octet-stream", "binary"),
    ],
)
def test_content_classification(content_type, expected):
    assert web_guard._classify(content_type) == expected
