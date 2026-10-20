"""Shared input contract for explicitly opted-in, attended local desktops.

Input still uses PyAutoGUI in phase 1. The independent platform drivers own
OS permission/display preflight, and native accessibility backends can replace
this transport without changing the model-visible ComputerUse schema.
"""
from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Protocol

from core.computer.accessibility import AccessibilityInspection, AccessibleElement


class DesktopDriver(Protocol):
    platform: str
    name: str

    def capabilities(self) -> dict[str, Any]: ...
    def size(self) -> tuple[int, int]: ...
    def move(self, x: int, y: int) -> None: ...
    def click(self, x: int, y: int, clicks: int) -> None: ...
    def scroll(self, delta_y: int) -> None: ...
    def type_text(self, text: str) -> None: ...
    def press_key(self, key: str) -> None: ...
    def screenshot(self, path: Path) -> None: ...
    def inspect_elements(self) -> AccessibilityInspection: ...
    def click_element(self, inspection: AccessibilityInspection, element: AccessibleElement) -> None: ...
    def set_element_text(self, inspection: AccessibilityInspection, element: AccessibleElement, text: str) -> None: ...
    def close(self) -> None: ...


class LocalDesktopBase:
    """Platform preflight plus basic PyAutoGUI input/MSS screenshot transport."""

    platform: str = ""
    name: str = ""
    display_server: str = "native"

    def __init__(self, *, environ: Mapping[str, str] | None = None) -> None:
        self._environ = dict(os.environ if environ is None else environ)

    def _check_available(self) -> None:
        raise NotImplementedError

    def capabilities(self) -> dict[str, Any]:
        return {
            "platform": self.platform,
            "driver": self.name,
            "display_server": self.display_server,
            "input_transport": "pyautogui",
            "screenshot_transport": "mss",
            "accessibility_tree": False,
            "background_input": False,
            "host_desktop": True,
            "supports": ["capabilities", "screenshot", "click", "move",
                         "scroll", "type", "key"],
        }

    def _gui(self):
        self._check_available()  # fail closed before ANY host input
        try:
            import pyautogui
        except ImportError as exc:
            raise RuntimeError(
                "ComputerUse requires optional pyautogui; install requirements-interactive-use.txt"
            ) from exc
        pyautogui.FAILSAFE = True
        pyautogui.PAUSE = 0.1
        return pyautogui

    def size(self) -> tuple[int, int]:
        width, height = self._gui().size()
        if width < 1 or height < 1:
            raise RuntimeError("Active desktop geometry unavailable")
        return int(width), int(height)

    def move(self, x: int, y: int) -> None:
        self._gui().moveTo(x, y, duration=0.15)

    def click(self, x: int, y: int, clicks: int) -> None:
        self._gui().click(x, y, clicks=clicks, interval=0.15)

    def scroll(self, delta_y: int) -> None:
        self._gui().scroll(delta_y)

    def type_text(self, text: str) -> None:
        self._gui().write(text, interval=0.01)

    def press_key(self, key: str) -> None:
        self._gui().press(key)

    def screenshot(self, path: Path) -> None:
        # Validate OS authorization even if input automation was never loaded.
        self._check_available()
        from mss import mss
        from PIL import Image
        with mss() as capture:
            monitors = capture.monitors
            if len(monitors) < 2:
                raise RuntimeError("No interactive display available")
            raw = capture.grab(monitors[1])
            image = Image.frombytes("RGB", raw.size, raw.rgb)
        image.thumbnail((1600, 1000))
        image.save(path, format="JPEG", quality=70)

    def inspect_elements(self) -> AccessibilityInspection:
        raise RuntimeError("Native accessibility backend is not installed")

    def click_element(
        self, inspection: AccessibilityInspection, element: AccessibleElement,
    ) -> None:
        raise RuntimeError("Native accessibility backend is not installed")

    def set_element_text(
        self, inspection: AccessibilityInspection, element: AccessibleElement, text: str,
    ) -> None:
        raise RuntimeError("Native accessibility backend is not installed")

    def close(self) -> None:
        """No shared OS input resource to close in the local adapter."""
