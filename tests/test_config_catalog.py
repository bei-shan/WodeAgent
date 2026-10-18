"""Project-scoped configuration use cases and unchanged HTTP contracts."""
from __future__ import annotations

import ast
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from fastapi.testclient import TestClient

from desktop.service.app import create_app
from desktop.service.application.config_catalog import ConfigCatalogService
from tests.test_web_service import FakeAgent


def test_mcp_config_crud_and_http_error_mapping(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("WEB_AUTH_TOKEN", raising=False)
    app = create_app(agent_factory=FakeAgent, project_root=str(tmp_path))
    with TestClient(app) as client:
        assert client.get("/api/mcp/servers").json() == []
        created = client.post("/api/mcp/servers", json={
            "name": "files", "command": "npx", "args": ["-y", "server"],
        })
        assert created.status_code == 201
        assert created.json() == {"status": "created", "name": "files"}
        assert client.get("/api/mcp/servers").json() == [{
            "name": "files", "command": "npx", "args": ["-y", "server"],
        }]
        assert client.post("/api/mcp/servers", json={
            "name": "files", "command": "python", "args": [],
        }).status_code == 409
        assert client.put("/api/mcp/servers/files", json={"command": "uvx"}).status_code == 200
        assert client.get("/api/mcp/servers").json()[0]["command"] == "uvx"
        assert client.put("/api/mcp/servers/missing", json={}).status_code == 404
        assert client.delete("/api/mcp/servers/files").status_code == 200
        assert client.delete("/api/mcp/servers/files").status_code == 404


def test_skill_and_mcp_toggles_persist_across_restarts(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("WEB_AUTH_TOKEN", raising=False)
    def new_client():
        return TestClient(create_app(agent_factory=FakeAgent, project_root=str(tmp_path)))
    with new_client() as client:
        assert client.get("/api/skills/example/enabled").json() == {
            "name": "example", "enabled": True,
        }
        assert client.put("/api/skills/example/enabled").json()["enabled"] is False
        assert client.put("/api/mcp/servers/local/enabled").json()["enabled"] is False
    with new_client() as client:
        assert client.get("/api/skills/example/enabled").json()["enabled"] is False
        assert client.get("/api/mcp/servers/local/enabled").json()["enabled"] is False
        assert client.put("/api/skills/example/enabled").json()["enabled"] is True
        assert client.put("/api/mcp/servers/local/enabled").json()["enabled"] is True
    data = json.loads((tmp_path / "skill_state.json").read_text())
    assert data.get("disabled_skills", {}) == {}
    assert data.get("disabled_mcp", {}) == {}


def test_hooks_roundtrip_and_no_half_written_json(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("WEB_AUTH_TOKEN", raising=False)
    with TestClient(create_app(agent_factory=FakeAgent, project_root=str(tmp_path))) as client:
        assert client.get("/api/hooks").json() == {"hooks": {}}
        saved = {"hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": []}]}}
        assert client.put("/api/hooks", json=saved).json() == {"status": "saved"}
        assert client.get("/api/hooks").json() == saved
    assert json.loads((tmp_path / ".mycode" / "hooks.json").read_text()) == saved


def test_concurrent_feature_state_toggles_do_not_lose_updates(tmp_path):
    service = ConfigCatalogService(tmp_path)
    # 20 concurrent toggles on distinct entries; a lost RMW write corrupts
    # the disabled map even if each individual HTTP response succeeds.
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(
            lambda i: service.toggle_enabled("skill", f"s{i}"),
            range(20),
        ))
    assert all(row["enabled"] is False for row in results)
    data = json.loads((tmp_path / "skill_state.json").read_text())
    assert len(data["disabled_skills"]) == 20
    assert not list(tmp_path.glob(".config-*.tmp"))


def test_editing_one_mcp_server_does_not_reformat_the_others(tmp_path):
    """mcp_servers.json is hand-edited and diff-reviewed, so a short args list
    must stay on its own line instead of being expanded by a rewrite."""
    config = tmp_path / "mcp_servers.json"
    config.write_text(
        '{\n  "mcpServers": {\n    "fetch": {\n      "command": "uvx",\n'
        '      "args": ["mcp-server-fetch"]\n    }\n  }\n}\n',
        encoding="utf-8",
    )
    service = ConfigCatalogService(tmp_path)

    service.create_mcp("extra", "npx", ["-y", "some-mcp-package"])

    text = config.read_text(encoding="utf-8")
    assert '"args": ["mcp-server-fetch"]' in text, text
    payload = json.loads(text)
    assert payload["mcpServers"]["extra"]["args"] == ["-y", "some-mcp-package"]
    assert payload["mcpServers"]["fetch"]["args"] == ["mcp-server-fetch"]


def test_long_lists_still_wrap_and_round_trip(tmp_path):
    """A list that cannot fit the line budget still renders as valid JSON."""
    config = tmp_path / "mcp_servers.json"
    service = ConfigCatalogService(tmp_path)
    long_args = [f"--option-number-{index:03d}" for index in range(12)]

    service.create_mcp("wide", "npx", long_args)

    text = config.read_text(encoding="utf-8")
    assert json.loads(text)["mcpServers"]["wide"]["args"] == long_args
    assert "\n" in text  # still a multi-line document


def test_application_service_has_no_transport_dependencies():
    root = Path(__file__).resolve().parents[1]
    module = root / "desktop/service/application/config_catalog.py"
    for node in ast.walk(ast.parse(module.read_text())):
        if isinstance(node, ast.ImportFrom):
            assert not (node.module or "").startswith(("fastapi", "starlette", "desktop.web", "tui"))
