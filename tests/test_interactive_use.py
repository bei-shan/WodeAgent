"""Opt-in local interactive drivers: discovery, boundaries and lifecycle."""
from __future__ import annotations

import json
import socket
import sys
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from core.config import Config
from core.runtime.service_interactive_use import InteractiveUseService
from core.tool_bootstrap import ToolBootstrap
from tools.builtin.browser_use import BrowserUseTool, validate_public_url
from tools.builtin.computer_use import ComputerUseTool
from tools.registry import ToolRegistry


def bootstrap(tmp_path, config):
    registry = ToolRegistry(config=config)
    b = ToolBootstrap(registry=registry, project_root=str(tmp_path))
    b.provide("config", config)
    return b, registry


def test_opt_in_disabled_and_sandbox_restricted(tmp_path):
    b, r = bootstrap(tmp_path, Config())
    names = b.discover_and_register()
    assert "BrowserUse" not in names
    assert "ComputerUse" not in names

    config = Config(
        sandbox_mode="docker", browser_use_enabled=True,
        computer_use_enabled=True, computer_use_host_acknowledge_risk=True,
    )
    b, r = bootstrap(tmp_path, config)
    names = b.discover_and_register()
    assert "BrowserUse" not in names
    assert "ComputerUse" not in names


def test_explicit_approval_only_advertises_local_tools(tmp_path):
    config = Config(
        browser_use_enabled=True, computer_use_enabled=True,
        computer_use_host_acknowledge_risk=True,
    )
    b, registry = bootstrap(tmp_path, config)
    names = b.discover_and_register()
    assert "BrowserUse" in names
    assert "ComputerUse" in names
    browser = registry.get_tool("BrowserUse")
    assert browser.get_parameters()[0].name == "action"
    assert registry.get_tool("ComputerUse") is not None
    browser.close()
    registry.get_tool("ComputerUse").close()


def test_computer_requires_both_gates(tmp_path):
    with pytest.raises(ValueError, match="opt-ins"):
        ComputerUseTool(tmp_path, Config(computer_use_enabled=True))
    with pytest.raises(ValueError, match="opt-ins"):
        ComputerUseTool(tmp_path, Config(
            computer_use_host_acknowledge_risk=True
        ))


@pytest.mark.parametrize("bad", [
    "file:///etc/passwd", "http://localhost/", "http://127.0.0.1/",
    "http://[::1]/", "http://169.254.169.254/latest/meta-data/",
    "http://10.0.0.1/", "https://metadata.google.internal/",
    "https://user:password@example.org/", "javascript:alert(1)",
])
def test_url_guard_denies_local_and_unsafe_schemes(bad):
    with pytest.raises(ValueError):
        validate_public_url(bad)


def test_url_guard_checks_every_resolved_address(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: [
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443)),
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.2.3.4", 443)),
    ])
    with pytest.raises(ValueError, match="Private"):
        validate_public_url("https://example.org")
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: [
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443)),
    ])
    assert validate_public_url("https://example.org/path") == "https://example.org/path"


def test_guard_denies_dns_failure_and_private_route(monkeypatch, tmp_path):
    def fail(*args, **kwargs):
        raise socket.gaierror("unavailable")
    monkeypatch.setattr(socket, "getaddrinfo", fail)
    with pytest.raises(ValueError, match="resolved"):
        validate_public_url("https://example.org")
    tool = BrowserUseTool(tmp_path, Config(browser_use_enabled=True))
    route = SimpleNamespace(
        request=SimpleNamespace(url="http://127.0.0.1/internal"),
        abort=Mock(), continue_=Mock(),
    )
    tool._on_request(route)
    route.abort.assert_called_once()
    route.continue_.assert_not_called()
    tool.close()


def test_browser_ref_invalid_and_network_denied_before_launch(tmp_path):
    tool = BrowserUseTool(tmp_path, Config(browser_use_enabled=True))
    denied = json.loads(tool.run({"action": "open", "url": "http://127.0.0.1/"}))
    assert denied["status"] == "error"
    assert tool._pw is None
    unknown = json.loads(tool.run({"action": "click", "ref": 99}))
    assert unknown["status"] == "error"
    tool.close()


def test_browser_run_snapshot_and_cleanup_without_installed_playwright(tmp_path):
    tool = BrowserUseTool(tmp_path, Config(browser_use_enabled=True))
    tool._act = lambda _: {
        "url": "https://example.org", "title": "Example",
        "elements": [{"ref": 1, "tag": "button", "label": "Next"}],
    }
    result = json.loads(tool.run({"action": "snapshot"}))
    assert result["status"] == "success"
    assert result["data"]["elements"][0]["ref"] == 1
    artifacts = tool._artifacts.name
    tool.close()
    from pathlib import Path
    assert not Path(artifacts).exists()


def test_native_driver_bounds_and_no_text_echo(tmp_path, monkeypatch):
    click = Mock()
    fake = SimpleNamespace(
        FAILSAFE=False, PAUSE=0, size=lambda: (100, 80),
        click=click, moveTo=Mock(), scroll=Mock(), write=Mock(),
        press=Mock(), screenshot=Mock(),
    )
    monkeypatch.setitem(sys.modules, "pyautogui", fake)
    tool = ComputerUseTool(tmp_path, Config(
        computer_use_enabled=True, computer_use_host_acknowledge_risk=True))
    bad = json.loads(tool.run({"action": "click", "x": 101, "y": 20}))
    assert bad["status"] == "error"
    assert click.call_count == 0
    good = json.loads(tool.run({"action": "click", "x": 10, "y": 20}))
    assert good["status"] == "success"
    assert click.call_count == 1
    typed = json.loads(tool.run({"action": "type", "text": "do not log me"}))
    assert typed["status"] == "success"
    assert "do not log me" not in json.dumps(typed)
    tool.close()


def test_interactive_service_closes_only_owned_tools():
    browser, computer = Mock(), Mock()
    agent = SimpleNamespace(
        tool_registry=SimpleNamespace(get_tool=lambda name: {
            "BrowserUse": browser, "ComputerUse": computer,
        }.get(name))
    )
    service = InteractiveUseService()
    service.start(agent)
    service.close(agent)
    browser.close.assert_called_once()
    computer.close.assert_called_once()
