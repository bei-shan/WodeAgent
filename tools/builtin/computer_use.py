"""Explicitly trusted HOST computer-use adapter (never a tenant sandbox).

This is intentionally separate from BrowserUse. For untrusted/cloud users use
a remote desktop VM driver with authenticated session/tenant-scoped channels.
"""
from __future__ import annotations

import tempfile
import time
import uuid
from pathlib import Path
from threading import RLock
from typing import Any

from tools.base import ErrorCode, Tool, ToolParameter


class ComputerUseTool(Tool):
    strict_input_contract = True
    usage_notes = (
        "ComputerUse operates the operator's actual desktop. Obtain user "
        "authorization for consequential actions (send, purchase, delete). "
        "Take a screenshot, act once, then verify with a fresh screenshot. "
        "Never use it to handle passwords, 2FA or CAPTCHA without a human."
    )

    def __init__(self, project_root=None, config=None):
        if not (
            config is not None
            and getattr(config, "computer_use_enabled", False)
            and getattr(config, "computer_use_host_acknowledge_risk", False)
            and getattr(config, "sandbox_mode", "local_trusted") == "local_trusted"
        ):
            raise ValueError("ComputerUse requires two explicit trusted-host opt-ins")
        super().__init__(
            name="ComputerUse",
            description="Operate the trusted local desktop: screenshot, click, move, scroll, type or key.",
            project_root=project_root,
        )
        self._lock = RLock()
        self._closed = False
        self._artifacts = tempfile.TemporaryDirectory(prefix="ridge-computer-use-")

    def get_parameters(self) -> list[ToolParameter]:
        return [
            ToolParameter(name="action", type="string", description="screenshot | click | move | scroll | type | key"),
            ToolParameter(name="x", type="integer", required=False, description="Screen X coordinate for click/move"),
            ToolParameter(name="y", type="integer", required=False, description="Screen Y coordinate for click/move"),
            ToolParameter(name="text", type="string", required=False, description="Plain text to type (max 500), or key name"),
            ToolParameter(name="clicks", type="integer", required=False, description="Mouse click count 1 or 2"),
            ToolParameter(name="delta_y", type="integer", required=False, description="Scroll steps in [-10, 10]"),
        ]

    def run(self, parameters: dict[str, Any]) -> str:
        started = time.monotonic()
        action = parameters.get("action")
        if action not in {"screenshot", "click", "move", "scroll", "type", "key"}:
            return self.create_error_response(
                ErrorCode.INVALID_PARAM, "Unsupported desktop action", parameters)
        try:
            with self._lock:
                if self._closed:
                    raise RuntimeError("ComputerUse closed")
                try:
                    import pyautogui
                except ImportError as exc:
                    raise RuntimeError("Install optional 'pyautogui' to use native computer control") from exc

                pyautogui.FAILSAFE = True  # mouse to screen corner aborts
                pyautogui.PAUSE = 0.1
                width, height = pyautogui.size()
                result: dict[str, Any] = {"width": width, "height": height, "action": action}
                if action in {"click", "move"}:
                    x, y = parameters.get("x"), parameters.get("y")
                    if type(x) is not int or type(y) is not int:
                        raise ValueError("x/y must be integer coordinates")
                    if not (0 <= x < width and 0 <= y < height):
                        raise ValueError("Coordinates out of current screen bounds")
                    if action == "move":
                        pyautogui.moveTo(x, y, duration=0.15)
                    else:
                        clicks = parameters.get("clicks", 1)
                        if type(clicks) is not int or clicks not in (1, 2):
                            raise ValueError("clicks must be 1 or 2")
                        pyautogui.click(x, y, clicks=clicks, interval=0.15)
                elif action == "scroll":
                    delta = parameters.get("delta_y")
                    if type(delta) is not int or abs(delta) > 10:
                        raise ValueError("delta_y must be an integer in [-10, 10]")
                    pyautogui.scroll(delta)
                elif action == "type":
                    typed = parameters.get("text")
                    if not isinstance(typed, str) or len(typed) > 500:
                        raise ValueError("text must be a string of at most 500 characters")
                    pyautogui.write(typed, interval=0.01)
                    result["typed_characters"] = len(typed)  # do not echo sensitive text
                elif action == "key":
                    key = parameters.get("text")
                    if key not in {"enter", "tab", "esc", "escape", "space", "backspace",
                                   "up", "down", "left", "right"}:
                        raise ValueError("Key is not in the safe allowlist")
                    pyautogui.press(key)
                else:
                    path = Path(self._artifacts.name) / (uuid.uuid4().hex + ".png")
                    image = pyautogui.screenshot()
                    image.save(path)
                    result["screenshot_path"] = str(path)
            msg = (
                "Desktop screenshot: " + result["screenshot_path"]
                if "screenshot_path" in result
                else "Desktop action sent; verify result with a fresh screenshot."
            )
            return self.create_success_response(
                data=result, text=msg, params_input=parameters,
                time_ms=int((time.monotonic() - started) * 1000))
        except ValueError as exc:
            return self.create_error_response(
                ErrorCode.INVALID_PARAM, str(exc), parameters,
                time_ms=int((time.monotonic() - started) * 1000))
        except Exception as exc:
            return self.create_error_response(
                ErrorCode.EXECUTION_ERROR,
                f"Desktop unavailable or action failed ({type(exc).__name__})",
                parameters, time_ms=int((time.monotonic() - started) * 1000))

    def close(self) -> None:
        with self._lock:
            if not self._closed:
                self._closed = True
                self._artifacts.cleanup()
