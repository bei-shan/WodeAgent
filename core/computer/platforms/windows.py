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
            from ctypes import wintypes

            class UserObjectFlags(ctypes.Structure):
                _fields_ = [
                    ("fInherit", wintypes.BOOL),
                    ("fReserved", wintypes.BOOL),
                    ("dwFlags", wintypes.DWORD),
                ]

            user32 = ctypes.windll.user32
            station = user32.GetProcessWindowStation()
            if not station:
                raise RuntimeError("Windows process has no interactive window station")
            flags = UserObjectFlags()
            needed = wintypes.DWORD()
            # UOI_FLAGS=1; WSF_VISIBLE=1 means an interactive station.
            if not user32.GetUserObjectInformationW(
                station, 1, ctypes.byref(flags),
                ctypes.sizeof(flags), ctypes.byref(needed)
            ):
                raise RuntimeError("Unable to verify interactive window station")
            if not flags.dwFlags & 1:
                raise RuntimeError("Windows session has a non-interactive window station")
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
