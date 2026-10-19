"""Native accessibility observations shared by macOS AX, Windows UIA, Linux AT-SPI.

Never serialize native handles, editable values, process details or passwords.
Native node identities are session-local and discarded after each action.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any


MAX_ELEMENTS = 64
MAX_VISITED = 240
MAX_DEPTH = 5

# Press is deliberately limited to actionable semantic roles. A press may
# nevertheless submit/delete/purchase, so the host's user approval still matters.
PRESS_ROLES = frozenset({
    "button", "push button", "check box", "checkbox", "radio button", "radiobutton",
    "link", "menu item", "menuitem", "tab item", "page tab", "tab", "toggle button",
})
EDIT_ROLES = frozenset({
    "text", "text field", "textfield", "text area", "textarea", "edit",
    "entry", "editcontrol", "edit control",
})


def safe_label(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    return " ".join(value.split())[:120]


def canonical_role(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    lowered = value.lower().replace("_", " ").strip()
    if lowered.startswith("ax"):
        lowered = lowered[2:]
    if lowered.endswith("control"):
        lowered = lowered[:-7]
    return lowered.strip()


@dataclass(frozen=True)
class AccessibleElement:
    handle: Any
    role: str
    label: str
    can_press: bool
    can_set_text: bool
    sensitive: bool = False

    def public(self, reference: str) -> dict[str, Any]:
        return {
            "ref": reference,
            "role": self.role,
            "label": "[protected]" if self.sensitive else safe_label(self.label),
            "actions": (
                (["click_element"] if self.can_press and not self.sensitive else [])
                + (["set_element_text"] if self.can_set_text and not self.sensitive else [])
            ),
        }


@dataclass(frozen=True)
class AccessibilityInspection:
    owner: Any
    elements: tuple[AccessibleElement, ...]
    truncated: bool = False

    def public(self, prefix: str) -> dict[str, Any]:
        return {
            "elements": [
                element.public(f"{prefix}:{index}")
                for index, element in enumerate(self.elements, start=1)
            ],
            "truncated": self.truncated,
            "values_included": False,
        }
