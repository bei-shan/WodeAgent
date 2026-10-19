"""macOS local driver: explicit TCC Accessibility check before host input.

Apple Accessibility tree (AXUIElement) and ScreenCaptureKit are separate native
capabilities for a later version; this phase uses PyAutoGUI + MSS transports.
"""
from __future__ import annotations

import ctypes
import importlib.util

from core.computer.accessibility import (
    AccessibilityInspection, AccessibleElement, EDIT_ROLES, PRESS_ROLES,
    MAX_ELEMENTS, MAX_VISITED, MAX_DEPTH, canonical_role, safe_label,
)

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

    def screenshot(self, path) -> None:
        # Query TCC permission; never request a grant automatically inside
        # the agent worker or capture a permission dialog as model context.
        try:
            framework = ctypes.cdll.LoadLibrary(
                "/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics"
            )
            preflight = framework.CGPreflightScreenCaptureAccess
            preflight.argtypes = []
            preflight.restype = ctypes.c_bool
            allowed = bool(preflight())
        except (AttributeError, OSError) as exc:
            raise RuntimeError("Cannot verify macOS Screen Recording permission") from exc
        if not allowed:
            raise RuntimeError(
                "macOS Screen Recording permission missing for the Ridge host app"
            )
        super().screenshot(path)

    @staticmethod
    def _ax_modules():
        try:
            import ApplicationServices as ax
            from AppKit import NSWorkspace
        except ImportError as exc:
            raise RuntimeError(
                "macOS native accessibility requires pyobjc-framework-ApplicationServices "
                "and pyobjc-framework-Cocoa"
            ) from exc
        return ax, NSWorkspace

    def _active_ax(self):
        self._check_available()
        ax, workspace = self._ax_modules()
        app = workspace.sharedWorkspace().frontmostApplication()
        if app is None:
            raise RuntimeError("No foreground macOS application")
        pid = int(app.processIdentifier())
        root = ax.AXUIElementCreateApplication(pid)
        ax.AXUIElementSetMessagingTimeout(root, 1.5)
        status, window = ax.AXUIElementCopyAttributeValue(
            root, ax.kAXFocusedWindowAttribute, None
        )
        if status != 0 or window is None:
            raise RuntimeError("Frontmost macOS application has no accessible focused window")
        return ax, pid, window

    @staticmethod
    def _ax_read(ax, node, name):
        try:
            status, value = ax.AXUIElementCopyAttributeValue(node, name, None)
            return value if status == 0 else None
        except Exception:
            return None

    @staticmethod
    def _ax_actions(ax, node):
        try:
            status, actions = ax.AXUIElementCopyActionNames(node, None)
            return list(actions or ()) if status == 0 else []
        except Exception:
            return []

    def inspect_elements(self) -> AccessibilityInspection:
        ax, pid, window = self._active_ax()
        elements = []
        pending = [(window, 0)]
        visited = 0
        while pending and visited < MAX_VISITED and len(elements) < MAX_ELEMENTS:
            node, depth = pending.pop()
            visited += 1
            role_raw = self._ax_read(ax, node, ax.kAXRoleAttribute) or ""
            role = canonical_role(role_raw)
            label = (
                self._ax_read(ax, node, ax.kAXTitleAttribute)
                or self._ax_read(ax, node, ax.kAXDescriptionAttribute)
                or self._ax_read(ax, node, "AXPlaceholderValue")
                or ""
            )
            label = safe_label(label)
            secure = "secure" in role or "password" in label.lower()
            if self._ax_read(ax, node, ax.kAXEnabledAttribute) is False:
                enabled = False
            else:
                enabled = True
            press = (
                enabled and not secure and role in PRESS_ROLES
                and ax.kAXPressAction in self._ax_actions(ax, node)
            )
            settable = False
            if enabled and not secure and role in EDIT_ROLES:
                try:
                    err, allowed = ax.AXUIElementIsAttributeSettable(
                        node, ax.kAXValueAttribute, None
                    )
                    settable = err == 0 and bool(allowed)
                except Exception:
                    settable = False
            if press or settable:
                elements.append(AccessibleElement(
                    handle=node, role=role, label=label,
                    can_press=press, can_set_text=settable, sensitive=secure,
                ))
            if depth < MAX_DEPTH:
                children = self._ax_read(ax, node, ax.kAXChildrenAttribute)
                if isinstance(children, (list, tuple)):
                    pending.extend((child, depth + 1) for child in reversed(children[:40]))
        return AccessibilityInspection(
            owner=(pid, window), elements=tuple(elements),
            truncated=bool(pending) or visited >= MAX_VISITED,
        )

    def _assert_native_target(self, inspection, element):
        ax, pid, window = self._active_ax()
        if pid != inspection.owner[0] or window != inspection.owner[1]:
            raise ValueError("Foreground macOS window changed; inspect again")
        role = canonical_role(self._ax_read(ax, element.handle, ax.kAXRoleAttribute))
        if role != element.role:
            raise ValueError("AX element changed or disappeared; inspect again")
        current_label = safe_label(
            self._ax_read(ax, element.handle, ax.kAXTitleAttribute)
            or self._ax_read(ax, element.handle, ax.kAXDescriptionAttribute)
            or self._ax_read(ax, element.handle, "AXPlaceholderValue")
            or ""
        )
        if current_label != element.label:
            raise ValueError("AX element label changed; inspect again")
        if self._ax_read(ax, element.handle, ax.kAXEnabledAttribute) is False:
            raise ValueError("AX element is disabled")
        return ax

    def click_element(self, inspection, element):
        if not element.can_press or element.sensitive:
            raise ValueError("Element does not expose a permitted native press action")
        ax = self._assert_native_target(inspection, element)
        if ax.kAXPressAction not in self._ax_actions(ax, element.handle):
            raise ValueError("Native press action no longer supported")
        if ax.AXUIElementPerformAction(element.handle, ax.kAXPressAction) != 0:
            raise RuntimeError("macOS AXPress failed")

    def set_element_text(self, inspection, element, text):
        if not element.can_set_text or element.sensitive:
            raise ValueError("Element does not expose editable non-secret text")
        ax = self._assert_native_target(inspection, element)
        err, allowed = ax.AXUIElementIsAttributeSettable(
            element.handle, ax.kAXValueAttribute, None
        )
        if err != 0 or not allowed:
            raise ValueError("Element is no longer writable")
        if ax.AXUIElementSetAttributeValue(
            element.handle, ax.kAXValueAttribute, text
        ) != 0:
            raise RuntimeError("macOS AXValue assignment failed")

    def capabilities(self):
        result = super().capabilities()
        available = bool(importlib.util.find_spec("ApplicationServices")) and bool(
            importlib.util.find_spec("AppKit")
        )
        result["accessibility_tree"] = available
        if available:
            result["supports"] += ["inspect_elements", "click_element", "set_element_text"]
        result["ready_hint"] = (
            "Requires macOS Accessibility and Screen Recording permissions. "
            "Native AXUIElement actions require optional PyObjC packages."
        )
        return result
