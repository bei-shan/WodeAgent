"""One model tool with strictly OS-selected, trusted-local desktop drivers.

Do not import this into hosted workers or substitute host input for guest VM
control. Each adapter owns its own permission/display policy and fails closed.
"""
from __future__ import annotations

import tempfile
import time
import uuid
from pathlib import Path
from threading import RLock
from typing import Any

from core.computer.platforms import create_native_desktop_driver
from tools.base import ErrorCode, Tool, ToolParameter


class ComputerUseTool(Tool):
    strict_input_contract = True
    usage_notes = (
        "ComputerUse acts on the operator's actual desktop. First call "
        "capabilities then screenshot to observe; act once and take a fresh "
        "screenshot to verify. Native Accessibility/UIA/AT-SPI element "
        "inspection is not provided in this phase. Only operate an attended "
        "trusted local host; high-impact actions need human authorization. "
        "Do not enter passwords, OTPs or CAPTCHA solutions."
    )

    def __init__(self, project_root=None, config=None):
        if not (
            config is not None
            and getattr(config, "computer_use_enabled", False) is True
            and getattr(config, "computer_use_host_acknowledge_risk", False) is True
            and getattr(config, "sandbox_mode", "local_trusted") == "local_trusted"
        ):
            raise ValueError("ComputerUse requires two explicit trusted-host opt-ins")
        super().__init__(
            name="ComputerUse",
            description=(
                "Inspect computer capabilities, capture the trusted local "
                "desktop, click, move, scroll, type or press a key. "
                "OS-specific drivers (macOS/Windows/Linux X11), never remote."
            ),
            project_root=project_root,
        )
        self._lock = RLock()
        self._closed = False
        self._driver = create_native_desktop_driver()
        self._artifacts = tempfile.TemporaryDirectory(prefix="ridge-computer-use-")

    def get_parameters(self) -> list[ToolParameter]:
        return [
            ToolParameter(
                name="action", type="string",
                description="capabilities | screenshot | click | move | scroll | type | key"
            ),
            ToolParameter(
                name="x", type="integer", required=False,
                description="Primary display X coordinate for click/move"
            ),
            ToolParameter(
                name="y", type="integer", required=False,
                description="Primary display Y coordinate for click/move"
            ),
            ToolParameter(
                name="text", type="string", required=False,
                description="Text to type (max 500) or one allowlisted key"
            ),
            ToolParameter(
                name="clicks", type="integer", required=False,
                description="Mouse click count 1 or 2"
            ),
            ToolParameter(
                name="delta_y", type="integer", required=False,
                description="Scroll steps in [-10, 10]"
            ),
        ]

    def run(self, parameters: dict[str, Any]) -> str:
        started = time.monotonic()
        action = parameters.get("action")
        safe_input = dict(parameters)
        if action == "type" and "text" in safe_input:
            safe_input["text"] = "[REDACTED]"
        if action not in {
            "capabilities", "screenshot", "click", "move", "scroll", "type", "key"
        }:
            return self.create_error_response(
                ErrorCode.INVALID_PARAM, "Unsupported desktop action", safe_input)
        try:
            with self._lock:
                if self._closed:
                    raise RuntimeError("ComputerUse closed")
                driver = self._driver
                if action == "capabilities":
                    result = driver.capabilities()
                else:
                    width, height = driver.size()
                    result: dict[str, Any] = {
                        "width": width, "height": height,
                        "action": action, "platform": driver.platform,
                        "driver": driver.name,
                    }
                    if action in {"click", "move"}:
                        x, y = parameters.get("x"), parameters.get("y")
                        if type(x) is not int or type(y) is not int:
                            raise ValueError("x/y must be integer coordinates")
                        if not (0 <= x < width and 0 <= y < height):
                            raise ValueError("Coordinates out of primary display bounds")
                        if action == "move":
                            driver.move(x, y)
                        else:
                            clicks = parameters.get("clicks", 1)
                            if type(clicks) is not int or clicks not in (1, 2):
                                raise ValueError("clicks must be 1 or 2")
                            driver.click(x, y, clicks)
                    elif action == "scroll":
                        delta = parameters.get("delta_y")
                        if type(delta) is not int or abs(delta) > 10:
                            raise ValueError("delta_y must be an integer in [-10, 10]")
                        driver.scroll(delta)
                    elif action == "type":
                        typed = parameters.get("text")
                        if not isinstance(typed, str) or len(typed) > 500:
                            raise ValueError("text must be a string of at most 500 characters")
                        driver.type_text(typed)
                        result["typed_characters"] = len(typed)
                    elif action == "key":
                        key = parameters.get("text")
                        if key not in {
                            "enter", "tab", "esc", "escape", "space", "backspace",
                            "up", "down", "left", "right"
                        }:
                            raise ValueError("Key is not in the safe allowlist")
                        driver.press_key(key)
                    else:
                        if len(list(Path(self._artifacts.name).glob("*.jpg"))) >= 20:
                            raise ValueError("Screenshot limit reached; close this Agent session")
                        path = Path(self._artifacts.name) / (uuid.uuid4().hex + ".jpg")
                        driver.screenshot(path)
                        result["screenshot_path"] = str(path)
            if action == "capabilities":
                msg = "Desktop driver capabilities returned; no input was sent."
            elif "screenshot_path" in result:
                msg = "Desktop screenshot: " + result["screenshot_path"]
            else:
                msg = "Desktop action sent; verify with a new screenshot."
            return self.create_success_response(
                data=result, text=msg, params_input=safe_input,
                time_ms=int((time.monotonic() - started) * 1000))
        except ValueError as exc:
            return self.create_error_response(
                ErrorCode.INVALID_PARAM, str(exc), safe_input,
                time_ms=int((time.monotonic() - started) * 1000))
        except Exception as exc:
            return self.create_error_response(
                ErrorCode.EXECUTION_ERROR,
                f"Desktop unavailable or action failed ({type(exc).__name__})",
                safe_input, time_ms=int((time.monotonic() - started) * 1000))

    def close(self) -> None:
        with self._lock:
            if not self._closed:
                self._closed = True
                try:
                    self._driver.close()
                finally:
                    self._artifacts.cleanup()
