"""Windows desktop driver: interactive desktop preflight, no Session-0 fallback.

Win32 UI Automation element actions are a future separate adapter. Phase 1
keeps PyAutoGUI/MSS for mouse, keyboard and capture, gated on interactive USER32.
"""
from __future__ import annotations

import ctypes
import importlib.util

from core.computer.accessibility import (
    AccessibilityInspection, AccessibleElement, EDIT_ROLES, PRESS_ROLES,
    MAX_ELEMENTS, MAX_VISITED, MAX_DEPTH, canonical_role, safe_label,
)

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
            user32.GetProcessWindowStation.argtypes = []
            user32.GetProcessWindowStation.restype = wintypes.HANDLE
            user32.GetUserObjectInformationW.argtypes = [
                wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p,
                wintypes.DWORD, ctypes.POINTER(wintypes.DWORD),
            ]
            user32.GetUserObjectInformationW.restype = wintypes.BOOL
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

    @staticmethod
    def _uia():
        try:
            import uiautomation as uia
        except ImportError as exc:
            raise RuntimeError(
                "Windows native UI Automation requires optional uiautomation package"
            ) from exc
        return uia

    def _active_uia(self):
        self._check_available()
        uia = self._uia()
        user32 = ctypes.windll.user32
        user32.GetForegroundWindow.argtypes = []
        user32.GetForegroundWindow.restype = ctypes.c_void_p
        hwnd = int(user32.GetForegroundWindow() or 0)
        if not hwnd:
            raise RuntimeError("No foreground Windows application")
        control = uia.ControlFromHandle(hwnd)
        if control is None:
            raise RuntimeError("Foreground window does not expose UI Automation")
        return uia, hwnd, control

    @staticmethod
    def _uia_role(control):
        return canonical_role(getattr(control, "ControlTypeName", ""))

    @staticmethod
    def _uia_label(control):
        return safe_label(getattr(control, "Name", "") or "")

    @staticmethod
    def _pattern(control, name):
        method = getattr(control, name, None)
        if not callable(method):
            return None
        try:
            return method()
        except Exception:
            return None

    def inspect_elements(self) -> AccessibilityInspection:
        uia, hwnd, window = self._active_uia()
        pending = [(window, 0)]
        elements = []
        visited = 0
        while pending and visited < MAX_VISITED and len(elements) < MAX_ELEMENTS:
            node, depth = pending.pop()
            visited += 1
            try:
                role = self._uia_role(node)
                label = self._uia_label(node)
                enabled = bool(getattr(node, "IsEnabled", False))
                sensitive = bool(getattr(node, "IsPassword", False)) or (
                    "password" in label.lower() or "secure" in role
                )
                invoke = self._pattern(node, "GetInvokePattern") if enabled else None
                toggle = self._pattern(node, "GetTogglePattern") if enabled else None
                can_press = (
                    not sensitive and role in PRESS_ROLES and
                    (invoke is not None or toggle is not None)
                )
                value = self._pattern(node, "GetValuePattern") if enabled else None
                can_write = (
                    not sensitive and role in EDIT_ROLES and
                    value is not None and not bool(value.IsReadOnly)
                )
                if can_press or can_write:
                    elements.append(AccessibleElement(
                        handle=node, role=role, label=label,
                        can_press=can_press, can_set_text=can_write,
                        sensitive=sensitive,
                    ))
                if depth < MAX_DEPTH:
                    child = node.GetFirstChildControl()
                    siblings = 0
                    new_children = []
                    while child is not None and siblings < 40:
                        new_children.append((child, depth + 1))
                        child = child.GetNextSiblingControl()
                        siblings += 1
                    pending.extend(reversed(new_children))
            except Exception:
                continue  # broken UIA provider; never fallback to coordinates
        return AccessibilityInspection(
            owner=hwnd, elements=tuple(elements),
            truncated=bool(pending) or visited >= MAX_VISITED,
        )

    def _assert_uia_target(self, inspection, element):
        _, hwnd, _ = self._active_uia()
        if hwnd != inspection.owner:
            raise ValueError("Foreground Windows app changed; inspect again")
        node = element.handle
        try:
            if (not node.Exists(0) or not node.IsEnabled
                    or self._uia_role(node) != element.role
                    or self._uia_label(node) != element.label
                    or node.IsPassword):
                raise ValueError("UIA element changed, is protected or is stale")
        except ValueError:
            raise
        except Exception as exc:
            raise ValueError("UIA element disappeared; inspect again") from exc
        return node

    def click_element(self, inspection, element):
        if not element.can_press or element.sensitive:
            raise ValueError("UIA element lacks a permitted activation pattern")
        node = self._assert_uia_target(inspection, element)
        invoke = self._pattern(node, "GetInvokePattern")
        if invoke is not None:
            invoke.Invoke()
            return
        toggle = self._pattern(node, "GetTogglePattern")
        if toggle is not None:
            toggle.Toggle()
            return
        raise ValueError("UIA action no longer supported; inspect again")

    def set_element_text(self, inspection, element, text):
        if not element.can_set_text or element.sensitive:
            raise ValueError("UIA element lacks a permitted edit pattern")
        node = self._assert_uia_target(inspection, element)
        value = self._pattern(node, "GetValuePattern")
        if value is None or value.IsReadOnly:
            raise ValueError("UIA edit pattern no longer writable")
        value.SetValue(text)

    def capabilities(self):
        result = super().capabilities()
        available = importlib.util.find_spec("uiautomation") is not None
        result["accessibility_tree"] = available
        if available:
            result["supports"] += ["inspect_elements", "click_element", "set_element_text"]
        result["ready_hint"] = (
            "Requires a signed-in interactive Windows desktop. "
            "DPI scaling/multi-display coordinates and UIA element actions "
            "need platform-specific acceptance."
        )
        return result
