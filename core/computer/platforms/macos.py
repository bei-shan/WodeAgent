"""macOS local driver: explicit TCC Accessibility check before host input.

Apple Accessibility tree (AXUIElement) and ScreenCaptureKit are separate native
capabilities for a later version; this phase uses PyAutoGUI + MSS transports.
"""
from __future__ import annotations

import ctypes

from core.computer.platforms.base import LocalDesktopBase


class MacOSDesktopDriver(LocalDesktopBase):
    platform = "darwin"
    name = "macos-local"
    display_server = "quartz"

    def _check_available(self) -> None:
        try:
            services = ctypes.cdll.LoadLibrary(
                "/System/Library/Frameworks/ApplicationServices.framework/ApplicationServices"
            )
            services.AXIsProcessTrusted.argtypes = []
            services.AXIsProcessTrusted.restype = ctypes.c_bool
            permitted = bool(services.AXIsProcessTrusted())
        except (AttributeError, OSError) as exc:
            raise RuntimeError("Cannot verify macOS Accessibility permission") from exc
        if not permitted:
            raise RuntimeError(
                "macOS Accessibility permission missing for the Ridge host app"
            )

    def capabilities(self):
        result = super().capabilities()
        result["ready_hint"] = (
            "Requires macOS Accessibility and Screen Recording permissions. "
            "Native AXUIElement element actions are not yet implemented."
        )
        return result
