"""Linux *X11-only* local driver, not a Wayland security bypass."""
from __future__ import annotations

import importlib.util

from core.computer.accessibility import (
    AccessibilityInspection, AccessibleElement, EDIT_ROLES, PRESS_ROLES,
    MAX_ELEMENTS, MAX_VISITED, MAX_DEPTH, canonical_role, safe_label,
)
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

    @staticmethod
    def _atspi():
        try:
            import pyatspi
        except ImportError as exc:
            raise RuntimeError(
                "Linux native accessibility requires pyatspi and an AT-SPI D-Bus bus"
            ) from exc
        return pyatspi

    @staticmethod
    def _state(node, pyatspi, state):
        try:
            return bool(node.getState().contains(state))
        except Exception:
            return False

    @staticmethod
    def _actions(node):
        try:
            action = node.queryAction()
            return {
                str(action.getName(i)).lower(): i
                for i in range(min(action.nActions, 20))
            }
        except Exception:
            return {}

    def _active_atspi(self):
        self._check_available()
        atspi = self._atspi()
        try:
            desktop = atspi.Registry.getDesktop(0)
        except Exception as exc:
            raise RuntimeError("AT-SPI desktop service unavailable") from exc
        # AT-SPI provider may enumerate unrelated apps. Only the ACTIVE top-
        # level window is inspected. Never return the whole desktop tree.
        for a in range(min(desktop.childCount, 50)):
            try:
                app = desktop.getChildAtIndex(a)
                for w in range(min(app.childCount, 40)):
                    window = app.getChildAtIndex(w)
                    if self._state(window, atspi, atspi.STATE_ACTIVE):
                        return atspi, window
            except Exception:
                continue
        raise RuntimeError("No active accessible AT-SPI window")

    @staticmethod
    def _role(node):
        return canonical_role(node.getRoleName())

    def inspect_elements(self) -> AccessibilityInspection:
        atspi, window = self._active_atspi()
        pending = [(window, 0)]
        elements = []
        visited = 0
        while pending and visited < MAX_VISITED and len(elements) < MAX_ELEMENTS:
            node, depth = pending.pop()
            visited += 1
            try:
                role = self._role(node)
                label = safe_label(node.name or "")
                enabled = self._state(node, atspi, atspi.STATE_SENSITIVE)
                sensitive = (
                    "password" in label.lower()
                    or "password" in role or "secret" in role
                )
                actions = self._actions(node) if enabled and not sensitive else {}
                can_press = (
                    role in PRESS_ROLES
                    and any(n in actions for n in ("click", "press", "activate", "toggle"))
                )
                editable = (
                    enabled and not sensitive and role in EDIT_ROLES
                    and self._state(node, atspi, atspi.STATE_EDITABLE)
                )
                if can_press or editable:
                    elements.append(AccessibleElement(
                        handle=node, role=role, label=label,
                        can_press=can_press, can_set_text=editable,
                        sensitive=sensitive,
                    ))
                if depth < MAX_DEPTH:
                    children = []
                    for i in range(min(node.childCount, 40)):
                        try:
                            children.append((node.getChildAtIndex(i), depth + 1))
                        except Exception:
                            continue
                    pending.extend(reversed(children))
            except Exception:
                continue
        return AccessibilityInspection(
            owner=window, elements=tuple(elements),
            truncated=bool(pending) or visited >= MAX_VISITED,
        )

    def _assert_atspi_target(self, inspection, element):
        atspi, window = self._active_atspi()
        if window != inspection.owner:
            raise ValueError("Foreground AT-SPI window changed; inspect again")
        node = element.handle
        try:
            role, label = self._role(node), safe_label(node.name or "")
        except Exception as exc:
            raise ValueError("AT-SPI node disappeared; inspect again") from exc
        if role != element.role or label != element.label:
            raise ValueError("AT-SPI node changed; inspect again")
        if not self._state(node, atspi, atspi.STATE_SENSITIVE):
            raise ValueError("AT-SPI element is disabled")
        return atspi, node

    def click_element(self, inspection, element):
        if not element.can_press or element.sensitive:
            raise ValueError("AT-SPI node lacks a permitted action")
        _, node = self._assert_atspi_target(inspection, element)
        actions = self._actions(node)
        for name in ("click", "press", "activate", "toggle"):
            if name in actions:
                if not node.queryAction().doAction(actions[name]):
                    raise RuntimeError("AT-SPI action was refused")
                return
        raise ValueError("AT-SPI action no longer available")

    def set_element_text(self, inspection, element, text):
        if not element.can_set_text or element.sensitive:
            raise ValueError("AT-SPI node lacks a permitted edit action")
        atspi, node = self._assert_atspi_target(inspection, element)
        if not self._state(node, atspi, atspi.STATE_EDITABLE):
            raise ValueError("AT-SPI element is no longer editable")
        node.queryEditableText().setTextContents(text)

    def capabilities(self):
        result = super().capabilities()
        result["display_server"] = (
            "wayland-unsupported" if (
                self._environ.get("XDG_SESSION_TYPE", "").lower() == "wayland"
                or self._environ.get("WAYLAND_DISPLAY")
            ) else "x11"
        )
        available = importlib.util.find_spec("pyatspi") is not None
        result["accessibility_tree"] = available and result["display_server"] == "x11"
        if result["accessibility_tree"]:
            result["supports"] += ["inspect_elements", "click_element", "set_element_text"]
        result["ready_hint"] = (
            "Wayland is not supported by the local coordinate driver"
            if result["display_server"] == "wayland-unsupported"
            else "Requires X11, DISPLAY, AT-SPI bus and optional pyatspi package"
        )
        return result
