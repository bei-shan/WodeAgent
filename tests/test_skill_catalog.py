"""Skill catalogue service ownership and workspace file boundaries."""
from __future__ import annotations

import ast
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from fastapi.testclient import TestClient

from desktop.service.app import create_app
from desktop.service.application.skill_catalog import SkillCatalogService
from tests.test_web_service import FakeAgent
from core.skills.skill_loader import SkillLoader


def _app(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("WEB_AUTH_TOKEN", raising=False)
    return create_app(agent_factory=FakeAgent, project_root=str(tmp_path))


def test_skill_crud_http_contract_and_atomic_updates(tmp_path, monkeypatch):
    with TestClient(_app(tmp_path, monkeypatch)) as client:
        body = {
            "name": "review-safety",
            "description": "Review tool security",
            "content": "# Checklist\n\nCheck filesystem boundaries.",
        }
        assert client.post("/api/skills/validate", json=body).json() == {
            "valid": True, "errors": [],
        }
        created = client.post("/api/skills", json=body)
        assert created.status_code == 201
        assert created.json() == {"status": "created", "name": "review-safety"}
        assert client.post("/api/skills", json=body).status_code == 409

        names = {row["name"] for row in client.get("/api/skills").json()}
        assert "review-safety" in names
        current = client.get("/api/skills/review-safety/content").json()
        assert current["description"] == "Review tool security"
        assert current["content"] == body["content"]
        assert current["frontmatter"]["name"] == "review-safety"

        update = client.put("/api/skills/review-safety", json={
            "description": "Review permissions",
            "content": "# Revised\nConfirm safe paths.",
        })
        assert update.status_code == 200
        current = client.get("/api/skills/review-safety/content").json()
        assert current["description"] == "Review permissions"
        assert current["content"] == "# Revised\nConfirm safe paths."
        doc = tmp_path / ".mycodeagent" / "skills" / "review-safety" / "SKILL.md"
        assert doc.exists()
        assert not list(doc.parent.glob(".SKILL-*.tmp"))
        assert client.delete("/api/skills/review-safety").json() == {
            "status": "deleted", "name": "review-safety",
        }
        assert not doc.exists()
        assert client.get("/api/skills/review-safety/content").status_code == 404
        assert client.delete("/api/skills/review-safety").status_code == 404


def test_builtin_source_skill_cannot_be_updated_or_deleted(tmp_path, monkeypatch):
    with TestClient(_app(tmp_path, monkeypatch)) as client:
        # Avoid auto-seeding source defaults by creating a runtime skill first.
        created = client.post("/api/skills", json={
            "name": "writable", "description": "Local", "content": "# Local",
        })
        assert created.status_code == 201
        source = tmp_path / "skills" / "built-in" / "SKILL.md"
        source.parent.mkdir(parents=True)
        text = "---\nname: built-in\ndescription: Shipped with project\n---\n# Original\n"
        source.write_text(text, encoding="utf-8")
        assert client.get("/api/skills/built-in/content").status_code == 200
        assert client.put("/api/skills/built-in", json={"content": "# Replaced"}).status_code == 403
        assert client.delete("/api/skills/built-in").status_code == 403
        assert source.read_text(encoding="utf-8") == text


def test_symlinked_skill_file_is_never_read_or_mutated(tmp_path, monkeypatch):
    outside = tmp_path.parent / (tmp_path.name + "-secret-skill")
    outside.mkdir()
    secret = outside / "SKILL.md"
    secret.write_text("---\nname: steal\ndescription: Secret\n---\nTOP SECRET\n")
    runtime = tmp_path / ".mycodeagent" / "skills"
    runtime.mkdir(parents=True)
    (runtime / "steal").symlink_to(outside, target_is_directory=True)
    with TestClient(_app(tmp_path, monkeypatch)) as client:
        assert "steal" not in {row["name"] for row in client.get("/api/skills").json()}
        assert client.get("/api/skills/steal/content").status_code == 404
        assert client.put("/api/skills/steal", json={"content": "tamper"}).status_code == 404
        assert client.delete("/api/skills/steal").status_code == 404
        assert secret.read_text().endswith("TOP SECRET\n")

    # Loader is shared by the Agent as well as the API.
    loader = SkillLoader(str(tmp_path))
    assert loader.get_skill("steal", refresh=True) is None


def test_symlinked_runtime_root_and_malicious_names_are_denied(tmp_path, monkeypatch):
    outside = tmp_path.parent / (tmp_path.name + "-runtime-external")
    outside.mkdir()
    project_data = tmp_path / ".mycodeagent"
    project_data.mkdir()
    (project_data / "skills").symlink_to(outside, target_is_directory=True)
    with TestClient(_app(tmp_path, monkeypatch)) as client:
        create = client.post("/api/skills", json={
            "name": "escape", "description": "Not allowed", "content": "body",
        })
        assert create.status_code == 403
        assert not (outside / "escape").exists()
        invalid = client.post("/api/skills", json={
            "name": "../escape", "description": "No", "content": "content",
        })
        assert invalid.status_code == 422
        invalid_desc = client.post("/api/skills", json={
            "name": "demo", "description": "first\nname: injected", "content": "body",
        })
        assert invalid_desc.status_code == 400


def test_two_concurrent_creates_do_not_overwrite(tmp_path):
    catalog = SkillCatalogService(tmp_path)
    def attempt(_):
        try:
            return catalog.create("same-name", "Secure", "# Good")["status"]
        except Exception as exc:
            return type(exc).__name__
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(attempt, range(2)))
    assert sorted(results) == ["ConflictError", "created"]
    path = tmp_path / ".mycodeagent" / "skills" / "same-name" / "SKILL.md"
    assert "name: same-name" in path.read_text()


def test_application_skill_catalog_import_boundary():
    path = Path(__file__).resolve().parents[1] / "desktop/service/application/skill_catalog.py"
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.ImportFrom):
            assert not (node.module or "").startswith(("fastapi", "starlette", "tui", "desktop.web"))
