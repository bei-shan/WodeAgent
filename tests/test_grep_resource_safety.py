"""Grep must not read private symlinks or accumulate unlimited subprocess output."""
from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import patch

import pytest

from core.runtime.process_execution import ProcessOutcome
from tools.builtin.search_code import GrepTool, RgOutputLimited


def call(tool, **kwargs):
    return json.loads(tool.run(kwargs))


@pytest.mark.skipif(not hasattr(os, "symlink"), reason="symlink not supported")
def test_grep_fallback_never_follows_outside_file_symlinks(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "private_secret.txt"
    outside.write_text("VERY_PRIVATE_SEARCH_NEEDLE\n")
    (workspace / "leak.txt").symlink_to(outside)
    (workspace / "safe.txt").write_text("not that phrase")
    tool = GrepTool(project_root=workspace)
    with patch("tools.builtin.search_code.shutil.which", return_value=None):
        result = call(tool, pattern="VERY_PRIVATE_SEARCH_NEEDLE")
    assert result["status"] in ("success", "partial")
    assert result["data"]["matches"] == []
    assert "VERY_PRIVATE_SEARCH_NEEDLE" not in result["text"].replace(
        "for 'VERY_PRIVATE_SEARCH_NEEDLE'", ""
    )


def test_grep_rg_output_limit_is_reported_partial(tmp_path):
    (tmp_path / "safe.txt").write_text("needle\n")
    tool = GrepTool(project_root=tmp_path)
    sample = json.dumps({
        "type": "match",
        "data": {
            "path": {"text": "safe.txt"},
            "lines": {"text": "needle\n"},
            "line_number": 1,
        },
    }) + "\n"
    process = ProcessOutcome(
        stdout=sample, stderr="", returncode=0,
        stdout_bytes=1000000, stderr_bytes=0, output_truncated=True,
    )
    with patch("tools.builtin.search_code.shutil.which", return_value="rg"), \
         patch("tools.builtin.search_code.run_process", return_value=process) as runner:
        result = call(tool, pattern="needle")
    assert result["status"] == "partial"
    assert result["data"]["matches"][0]["file"] == "safe.txt"
    assert "output was capped" in result["text"]
    assert runner.call_args.kwargs["max_output_bytes"] == tool.MAX_RG_OUTPUT_BYTES


def test_grep_untrusted_rg_json_cannot_publish_private_path(tmp_path):
    tool = GrepTool(project_root=tmp_path)
    outside = (tmp_path.parent / "other-private.txt").resolve()
    payload = json.dumps({
        "type": "match",
        "data": {
            "path": {"text": str(outside)},
            "lines": {"text": "secrets\n"},
            "line_number": 1,
        },
    })
    assert tool._parse_rg_json_output(payload) == []


def test_grep_python_fallback_truncates_long_match_text(tmp_path):
    (tmp_path / "large.txt").write_text("needle" + "x" * 100000)
    tool = GrepTool(project_root=tmp_path)
    with patch("tools.builtin.search_code.shutil.which", return_value=None):
        result = call(tool, pattern="needle")
    assert len(result["data"]["matches"][0]["text"]) == tool.MAX_MATCH_TEXT


def test_grep_rejects_nonstring_path_before_resolve(tmp_path):
    tool = GrepTool(project_root=tmp_path)
    for path in (42, ["."], "\x00"):
        result = call(tool, pattern="needle", path=path)
        assert result["status"] == "error"
        assert result["error"]["code"] == "INVALID_PARAM"
