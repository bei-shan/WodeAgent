"""OS-specific desktop drivers; no fallback to another host's backend."""
from __future__ import annotations

import sys
from collections.abc import Mapping

from core.computer.platforms.base import DesktopDriver


def create_native_desktop_driver(
    *, platform: str | None = None, environ: Mapping[str, str] | None = None,
) -> DesktopDriver:
    """Select exactly one OS backend. Overrides are for trusted tests only.

    Neither platform nor environment may be selected by model tool arguments.
    Missing/unsupported platforms fail closed; never fall back to host shell.
    """
    system = sys.platform if platform is None else platform
    if system == "darwin":
        from core.computer.platforms.macos import MacOSDesktopDriver
        return MacOSDesktopDriver(environ=environ)
    if system == "win32":
        from core.computer.platforms.windows import WindowsDesktopDriver
        return WindowsDesktopDriver(environ=environ)
    if system == "linux":
        from core.computer.platforms.linux import LinuxX11DesktopDriver
        return LinuxX11DesktopDriver(environ=environ)
    raise RuntimeError(f"ComputerUse has no supported local driver for {system!r}")
