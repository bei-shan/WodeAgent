"""Windows desktop driver: interactive desktop preflight, no Session-0 fallback.

Win32 UI Automation element actions are a future separate adapter. Phase 1
keeps PyAutoGUI/MSS for mouse, keyboard and capture, gated on interactive USER32.
"""
from __future__ import annotations

import ctypes

from core.computer.platforms.base import LocalDesktopBase


class WindowsDesktopDriver(LocalDesktopBase):
    platform = "win32"
    name = "windows-local"
    display_server = "win32"

    def _check_available(self) -> None:
        try:
            user32 = ctypes.windll.user32
            # Services and headless workers with no desktop cannot send global input.
            if not user32.GetProcessWindowStation():
                raise RuntimeError("Windows process has no interactive window station")
        except AttributeError as exc:
            raise RuntimeError("Windows USER32 desktop APIs unavailable") from exc

    def capabilities(self):
        result = super().capabilities()
        result["ready_hint"] = (
            "Requires a signed-in interactive Windows desktop. "
            "DPI scaling/multi-display coordinates and UIA element actions "
            "need platform-specific acceptance."
        )
        return result
