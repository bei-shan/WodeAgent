"""Cross-tool file publication semantics: secure temp, mode and no-clobber."""
from __future__ import annotations

import json
import os
import stat
from unittest.mock import patch

import pytest

from core.runtime.file_commit import ConcurrentFileChange, commit_utf8
from tools.builtin.write_file import WriteTool
from tools.builtin.edit_file import EditTool
from tools.builtin.edit_file_multi import MultiEditTool


def invoke(tool, parameters):
    return json.loads(tool.run(parameters))


def expected(target):
    s = target.stat()
    return {"expected_mtime_ms": s.st_mtime_ns // 1_000_000,
            "expected_size_bytes": s.st_size}


def test_exclusive_create_prevents_clobber(tmp_path, monkeypatch):
    target = tmp_path / "new.txt"
    real_link = os.link

    def create_during_commit(src, dest, *args, **kwargs):
        target.write_text("other agent owns this file")
        return real_link(src, dest, *args, **kwargs)

    monkeypatch.setattr(os, "link", create_during_commit)
    with pytest.raises(ConcurrentFileChange):
        commit_utf8(target, "my content", expected_stat=None)
    assert target.read_text() == "other agent owns this file"
    assert list(tmp_path.glob("*.ridge-write")) == []
    assert list(tmp_path.glob(".*.ridge-write")) == []


def test_precise_conflict_detects_change_even_with_restored_mtime(tmp_path):
    target = tmp_path / "same.txt"
    target.write_text("one")
    before = target.stat()
    target.write_text("two")  # same length; mtime restored on purpose
    os.utime(target, ns=(before.st_atime_ns, before.st_mtime_ns))
    with pytest.raises(ConcurrentFileChange):
        commit_utf8(target, "three", expected_stat=before)
    assert target.read_text() == "two"


@pytest.mark.skipif(os.name == "nt", reason="POSIX executable bit semantics")
@pytest.mark.parametrize("tool_class,parameters", [
    (WriteTool, {"content": "beta\n"}),
    (EditTool, {"old_string": "alpha", "new_string": "beta"}),
    (MultiEditTool, {"edits": [{"old_string": "alpha", "new_string": "beta"}]}),
])
def test_existing_file_mode_preserved_across_all_edit_tools(
    tmp_path, tool_class, parameters,
):
    target = tmp_path / "script.sh"
    target.write_text("alpha\n")
    target.chmod(0o750)
    tool = tool_class(project_root=tmp_path)
    result = invoke(tool, {"path": "script.sh", **expected(target), **parameters})
    assert result["status"] in ("success", "partial"), result
    assert target.read_text() == "beta\n"
    assert stat.S_IMODE(target.stat().st_mode) == 0o750


@pytest.mark.parametrize("tool_class,parameters", [
    (WriteTool, {"content": "beta\n"}),
    (EditTool, {"old_string": "alpha", "new_string": "beta"}),
    (MultiEditTool, {"edits": [{"old_string": "alpha", "new_string": "beta"}]}),
])
def test_all_file_tools_return_conflict_when_commit_detects_race(
    tmp_path, tool_class, parameters,
):
    target = tmp_path / "file.txt"
    target.write_text("alpha\n")
    tool = tool_class(project_root=tmp_path)
    module = tool_class.__module__
    with patch(f"{module}.commit_utf8", side_effect=ConcurrentFileChange("raced")):
        result = invoke(tool, {"path": "file.txt", **expected(target), **parameters})
    assert result["status"] == "error", result
    assert result["error"]["code"] == "CONFLICT"
    assert target.read_text() == "alpha\n"


def test_write_new_file_creates_exclusively(tmp_path):
    tool = WriteTool(project_root=tmp_path)
    result = invoke(tool, {"path": "new.txt", "content": "content\n"})
    assert result["status"] == "success"
    assert (tmp_path / "new.txt").read_text() == "content\n"
    assert not list(tmp_path.glob(".new.txt.*.ridge-write"))
