"""Linux *X11-only* local driver, not a Wayland security bypass."""
from __future__ import annotations

from core.computer.platforms.base import LocalDesktopBase


class LinuxX11DesktopDriver(LocalDesktopBase):
    platform = "linux"
    name = "linux-x11"
    display_server = "x11"

    def _check_available(self) -> None:
        session = self._environ.get("XDG_SESSION_TYPE", "").lower().strip()
        if session == "wayland" or self._environ.get("WAYLAND_DISPLAY"):
            raise RuntimeError(
                "Linux native Wayland global desktop input is unsupported. "
                "Use an explicitly authorized xdg-desktop-portal/AT-SPI driver "
                "or run inside a disposable X11 desktop; no XWayland fallback."
            )
        if not self._environ.get("DISPLAY"):
            raise RuntimeError(
                "Linux ComputerUse needs an attended X11 DISPLAY or isolated Xvfb desktop"
            )

    def capabilities(self):
        result = super().capabilities()
        result["display_server"] = (
            "wayland-unsupported" if (
                self._environ.get("XDG_SESSION_TYPE", "").lower() == "wayland"
                or self._environ.get("WAYLAND_DISPLAY")
            ) else "x11"
        )
        result["ready_hint"] = (
            "Wayland is not supported by the local coordinate driver"
            if result["display_server"] == "wayland-unsupported"
            else "Requires an attended X11 session and DISPLAY"
        )
        return result
