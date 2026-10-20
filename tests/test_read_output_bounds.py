"""Read must not load every line (or a giant single line) into memory."""
from __future__ import annotations

import json

import pytest

from tools.builtin.read_file import ReadTool


def _response(tool, **kwargs):
    return json.loads(tool.run(kwargs))


def test_giant_single_line_is_capped_before_response(tmp_path):
    path = tmp_path / "giant.txt"
    with path.open("wb") as file:
        for _ in range(512):
            file.write(b"a" * 8192)
    tool = ReadTool(project_root=tmp_path)
    result = _response(tool, path="giant.txt", limit=1)
    assert result["status"] == "partial"
    assert result["data"]["truncated"] is True
    assert result["stats"]["total_lines"] == 1
    assert result["stats"]["lines_read"] == 1
    assert "Content capped" in result["text"]
    assert len(result["data"]["content"]) <= tool.MAX_LINE_CHARS + 20
    assert len(json.dumps(result)) < 10000


def test_many_lines_observation_is_capped_and_pagination_is_correct(tmp_path):
    path = tmp_path / "many.txt"
    path.write_text(("x" * 300 + "\n") * 6000)
    tool = ReadTool(project_root=tmp_path)
    result = _response(tool, path="many.txt", limit=2000, start_line=1)
    assert result["status"] == "partial"
    assert result["data"]["truncated"] is True
    assert result["stats"]["total_lines"] == 6000
    assert 1 <= result["stats"]["lines_read"] < 2000
    assert "Use start_line=" in result["text"]
    assert len(result["data"]["content"]) <= tool.MAX_CONTENT_CHARS
    late = _response(tool, path="many.txt", limit=2, start_line=5999)
    assert late["stats"]["total_lines"] == 6000
    assert late["data"]["truncated"] is False
    assert "5999 |" in late["data"]["content"]
    assert "6000 |" in late["data"]["content"]


def test_invalid_utf8_outside_selected_window_still_marks_fallback(tmp_path):
    (tmp_path / "bad.txt").write_bytes(b"first\nsecond\n" + b"\xff" + b"\n")
    tool = ReadTool(project_root=tmp_path)
    result = _response(tool, path="bad.txt", limit=1)
    assert result["status"] == "partial"
    assert result["data"]["fallback_encoding"] == "replace"
    assert result["data"]["truncated"] is True
    assert result["stats"]["total_lines"] == 3


def test_file_size_cap_denies_extreme_files_without_allocating(tmp_path):
    tool = ReadTool(project_root=tmp_path)
    path = tmp_path / "huge.txt"
    with path.open("wb") as file:
        file.truncate(tool.MAX_FILE_BYTES + 1)
    result = _response(tool, path="huge.txt")
    assert result["status"] == "error"
    assert result["error"]["code"] == "INVALID_PARAM"
    assert "Grep" in result["error"]["message"]


@pytest.mark.parametrize("kwargs", [
    {"path": "a.txt", "start_line": True},
    {"path": "a.txt", "limit": True},
    {"path": "a.txt", "limit": 2001},
])
def test_invalid_read_args_do_not_scan(tmp_path, kwargs):
    (tmp_path / "a.txt").write_text("hello")
    tool = ReadTool(project_root=tmp_path)
    result = _response(tool, **kwargs)
    assert result["status"] == "error"
    assert result["error"]["code"] == "INVALID_PARAM"
