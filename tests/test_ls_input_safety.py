"""LS options are strict and never crash on malformed tool input."""
from __future__ import annotations

import json

from tools.builtin.list_files import ListFilesTool


def test_ls_strict_input_types(tmp_path):
    tool = ListFilesTool(project_root=tmp_path)
    for params in [
        {"path": []},
        {"path": "\x00"},
        {"offset": True},
        {"limit": False},
        {"include_hidden": "false"},
        {"ignore": ["*.py", 42]},
        {"ignore": ["x" * 300]},
        {"ignore": ["x"] * 101},
    ]:
        result = json.loads(tool.run(params))
        assert result["status"] == "error"
        assert result["error"]["code"] == "INVALID_PARAM"


def test_ls_existing_pagination_preserved(tmp_path):
    for i in range(5):
        (tmp_path / f"item{i}.txt").write_text(str(i))
    tool = ListFilesTool(project_root=tmp_path)
    result = json.loads(tool.run({"offset": 1, "limit": 2}))
    assert result["status"] == "partial"
    assert len(result["data"]["entries"]) == 2
    assert result["data"]["entries"][0]["path"] == "item1.txt"
