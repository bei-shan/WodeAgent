"""Disposable Xvfb VM-like display smoke for explicit native desktop control.

Never point this test at a real user's desktop: it clicks/presses keys.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from core.config import Config
from tools.builtin.computer_use import ComputerUseTool


@pytest.mark.skipif(
    os.getenv("RIDGE_COMPUTER_GUI_SMOKE") != "1",
    reason="requires disposable Xvfb display and explicit test opt-in",
)
def test_real_desktop_driver_under_disposable_xvfb(tmp_path):
    tool = ComputerUseTool(tmp_path, Config(
        computer_use_enabled=True,
        computer_use_host_acknowledge_risk=True,
    ))
    try:
        def call(**args):
            result = json.loads(tool.run(args))
            assert result["status"] == "success", result
            return result["data"]

        shot = call(action="screenshot")
        assert Path(shot["screenshot_path"]).is_file()
        assert shot["width"] >= 200 and shot["height"] >= 200
        call(action="move", x=80, y=80)
        call(action="click", x=80, y=80)
        call(action="scroll", delta_y=-1)
        call(action="key", text="tab")
        call(action="type", text="hello")
    finally:
        tool.close()
