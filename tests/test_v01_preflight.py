"""Offline runtime release preflight contracts and safe machine-readable output."""
from __future__ import annotations

import json

from scripts import v01_preflight as preflight


def _root(tmp_path):
    (tmp_path / "requirements.txt").write_text("fastapi>=0.110\n")
    (tmp_path / "desktop/web").mkdir(parents=True)
    (tmp_path / "desktop/web/package-lock.json").write_text("{}")
    (tmp_path / "tui-v2").mkdir()
    (tmp_path / "tui-v2/bun.lock").write_text("")
    return tmp_path


def test_core_preflight_passes_with_valid_runtime_and_installed_requirements(tmp_path):
    checks = preflight.collect(
        "core", root=_root(tmp_path), python_version=(3, 12, 2),
        has_package=lambda _: True,
        get_version=lambda _: None,
    )
    assert all(check.ok for check in checks)
    assert "bun" not in [check.id for check in checks]
    assert "node" not in [check.id for check in checks]


def test_core_preflight_fails_if_package_or_python_missing(tmp_path):
    checks = preflight.collect(
        "core", root=_root(tmp_path), python_version=(3, 11, 9),
        has_package=lambda name: name != "mcp",
    )
    assert {item.id for item in checks if not item.ok} == {"python", "python:mcp"}


def test_web_and_tui_dependencies_are_scoped(tmp_path):
    root = _root(tmp_path)
    versions = {"node": (20, 15, 1), "npm": (10, 2, 0), "bun": (1, 4, 2)}
    get = versions.get
    web = preflight.collect("web", root=root, python_version=(3, 12),
                             has_package=lambda _: True, get_version=get)
    tui = preflight.collect("tui", root=root, python_version=(3, 12),
                             has_package=lambda _: True, get_version=get)
    assert all(check.ok for check in web)
    assert all(check.ok for check in tui)
    assert "node" in [check.id for check in web]
    assert "bun" not in [check.id for check in web]
    assert "bun" in [check.id for check in tui]
    assert "node" not in [check.id for check in tui]


def test_tui_missing_dependency_lockfile_and_old_bun_fail(tmp_path):
    root = _root(tmp_path)
    (root / "tui-v2/bun.lock").unlink()
    checks = preflight.collect(
        "tui", root=root, python_version=(3, 12),
        has_package=lambda _: True, get_version=lambda name: (1, 3, 0),
    )
    assert {item.id for item in checks if not item.ok} == {"bun", "tui-lockfile"}


def test_json_output_has_stable_status_without_environment_dump(capsys, monkeypatch):
    monkeypatch.setattr(preflight, "collect", lambda target: [
        preflight.Check("python", True, "Python 3.12+ required"),
        preflight.Check("python:mcp", False, "Required Python package: mcp"),
    ])
    assert preflight.main(["--target", "core", "--json"]) == 1
    obj = json.loads(capsys.readouterr().out)
    assert obj["ok"] is False
    assert obj["target"] == "core"
    assert [x["id"] for x in obj["checks"]] == ["python", "python:mcp"]
    assert all(set(x) == {"id", "ok", "detail"} for x in obj["checks"])


def test_command_version_parser_is_bounded_and_returns_none_when_missing(monkeypatch):
    monkeypatch.setattr(preflight.shutil, "which", lambda _: None)
    assert preflight._version("missing") is None
    assert preflight._VERSION_RE.search("bun v1.4.2").groups() == ("1", "4", "2")
