"""Glob argument contract and workspace symlink safety."""
from __future__ import annotations

import json
import os
import pytest

from tools.builtin.search_files_by_name import SearchFilesByNameTool


def test_glob_strict_optional_arguments(tmp_path):
    tool = SearchFilesByNameTool(project_root=tmp_path)
    for args in [
        {"pattern": 5},
        {"pattern": []},
        {"pattern": "*.py", "path": {}},
        {"pattern": "*.py", "limit": True},
        {"pattern": "*.py", "include_hidden": "false"},
        {"pattern": "*.py", "include_ignored": 1},
    ]:
        out = json.loads(tool.run(args))
        assert out["status"] == "error"
        assert out["error"]["code"] == "INVALID_PARAM"


@pytest.mark.skipif(not hasattr(os, "symlink"), reason="symlink unavailable")
def test_glob_never_returns_files_symlinked_outside_workspace(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "secret.txt"
    outside.write_text("private")
    (workspace / "secret-link.txt").symlink_to(outside)
    (workspace / "safe.txt").write_text("hello")
    tool = SearchFilesByNameTool(project_root=workspace)
    out = json.loads(tool.run({"pattern": "*.txt"}))
    assert out["status"] in ("success", "partial")
    assert out["data"]["paths"] == ["safe.txt"]
