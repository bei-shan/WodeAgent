"""Native AX / UIA / AT-SPI adapter contracts, isolated from the real desktop."""
from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from core.computer.accessibility import AccessibleElement, AccessibilityInspection
from core.computer.platforms.macos import MacOSDesktopDriver
from core.computer.platforms.windows import WindowsDesktopDriver
from core.computer.platforms.linux import LinuxX11DesktopDriver
from core.config import Config
from tools.builtin import computer_use as tool_mod


def tool_with_driver(monkeypatch, tmp_path, driver):
    monkeypatch.setattr(tool_mod, "create_native_desktop_driver", lambda: driver)
    return tool_mod.ComputerUseTool(tmp_path, Config(
        computer_use_enabled=True,
        computer_use_host_acknowledge_risk=True,
    ))


def call(tool, action, **kwargs):
    return json.loads(tool.run({"action": action, **kwargs}))


def fake_driver():
    button = AccessibleElement(object(), "button", "Continue", True, False)
    edit = AccessibleElement(object(), "edit", "Search", False, True)
    secret = AccessibleElement(object(), "edit", "Password", False, False, True)
    driver = Mock()
    driver.platform = "linux"
    driver.name = "mock-atspi"
    driver.size.return_value = (1200, 800)
    driver.inspect_elements.return_value = AccessibilityInspection(
        "application-A", (button, edit, secret)
    )
    return driver


def test_one_shot_native_element_refs_do_not_leak_handles(monkeypatch, tmp_path):
    driver = fake_driver()
    tool = tool_with_driver(monkeypatch, tmp_path, driver)
    try:
        page = call(tool, "inspect_elements")
        assert page["status"] == "success"
        elements = page["data"]["elements"]
        assert len(elements) == 3
        assert all("handle" not in element for element in elements)
        assert all("value" not in element for element in elements)
        assert page["data"]["values_included"] is False
        assert elements[2]["label"] == "[protected]"
        assert elements[2]["actions"] == []
        r1, r2 = elements[0]["ref"], elements[1]["ref"]
        assert call(tool, "click_element", element_ref=r1)["status"] == "success"
        assert driver.click_element.call_count == 1
        assert call(tool, "click_element", element_ref=r1)["status"] == "error"
        assert call(tool, "set_element_text", element_ref=r2, text="sensitive")["status"] == "error"
        assert driver.set_element_text.call_count == 0
        fresh = call(tool, "inspect_elements")["data"]["elements"][1]["ref"]
        result = call(tool, "set_element_text", element_ref=fresh, text="not-logged")
        assert result["status"] == "success"
        assert "not-logged" not in json.dumps(result)
        assert result["data"]["typed_characters"] == 10
        driver.set_element_text.assert_called_once()
    finally:
        tool.close()


def test_native_ref_invalidated_by_all_desktop_mutations(monkeypatch, tmp_path):
    driver = fake_driver()
    tool = tool_with_driver(monkeypatch, tmp_path, driver)
    try:
        ref = call(tool, "inspect_elements")["data"]["elements"][0]["ref"]
        assert call(tool, "move", x=20, y=20)["status"] == "success"
        assert call(tool, "click_element", element_ref=ref)["status"] == "error"
        ref2 = call(tool, "inspect_elements")["data"]["elements"][0]["ref"]
        ref3 = call(tool, "inspect_elements")["data"]["elements"][0]["ref"]
        assert ref2 != ref3
        assert call(tool, "click_element", element_ref=ref2)["status"] == "error"
        assert call(tool, "click_element", element_ref=ref3)["status"] == "success"
    finally:
        tool.close()


def test_native_failed_execution_consumes_ref_and_redacts_text(monkeypatch, tmp_path):
    driver = fake_driver()
    driver.set_element_text.side_effect = RuntimeError("remote provider failed")
    tool = tool_with_driver(monkeypatch, tmp_path, driver)
    try:
        ref = call(tool, "inspect_elements")["data"]["elements"][1]["ref"]
        result = call(tool, "set_element_text", element_ref=ref, text="private-token")
        assert result["status"] == "error"
        assert "private-token" not in json.dumps(result)
        assert call(tool, "set_element_text", element_ref=ref, text="private-token")["status"] == "error"
        assert driver.set_element_text.call_count == 1
    finally:
        tool.close()


class AxNode:
    def __init__(self, **attrs):
        self.attrs = attrs


def ax_module():
    mod = SimpleNamespace(
        kAXRoleAttribute="role", kAXTitleAttribute="title",
        kAXDescriptionAttribute="description", kAXEnabledAttribute="enabled",
        kAXChildrenAttribute="children", kAXValueAttribute="value",
        kAXPressAction="AXPress",
    )
    mod.AXUIElementCopyAttributeValue = lambda node, attr, unused: (
        (0, node.attrs[attr]) if attr in node.attrs else (1, None)
    )
    mod.AXUIElementCopyActionNames = lambda node, unused: (0, node.attrs.get("actions", []))
    mod.AXUIElementIsAttributeSettable = lambda node, attr, unused: (
        0, node.attrs.get("editable", False)
    )
    mod.AXUIElementPerformAction = Mock(return_value=0)
    mod.AXUIElementSetAttributeValue = Mock(return_value=0)
    return mod


def test_macos_ax_native_press_edit_and_focus_fencing(monkeypatch):
    mod = ax_module()
    button = AxNode(role="AXButton", title="Go", enabled=True, actions=["AXPress"])
    edit = AxNode(role="AXTextField", title="Query", enabled=True, editable=True)
    pwd = AxNode(role="AXSecureTextField", title="Private", enabled=True, editable=True)
    root = AxNode(role="AXWindow", children=[button, edit, pwd])
    driver = MacOSDesktopDriver(environ={})
    monkeypatch.setattr(driver, "_active_ax", lambda: (mod, 1001, root))
    inspection = driver.inspect_elements()
    assert [e.role for e in inspection.elements] == ["button", "textfield"]
    driver.click_element(inspection, inspection.elements[0])
    mod.AXUIElementPerformAction.assert_called_once_with(button, mod.kAXPressAction)
    driver.set_element_text(inspection, inspection.elements[1], "HELLO")
    mod.AXUIElementSetAttributeValue.assert_called_once_with(edit, "value", "HELLO")
    monkeypatch.setattr(driver, "_active_ax", lambda: (mod, 1002, root))
    with pytest.raises(ValueError, match="Foreground"):
        driver.click_element(inspection, inspection.elements[0])


class UiaNode:
    def __init__(self, role, name, *, password=False, editable=False, invoke=False):
        self.ControlTypeName = role
        self.Name = name
        self.IsEnabled = True
        self.IsPassword = password
        self._editable = editable
        self._invoke = invoke
        self.invocations = 0
        self.writes = []
        self.children = []
        self.next = None

    def GetFirstChildControl(self):
        return self.children[0] if self.children else None

    def GetNextSiblingControl(self):
        return self.next

    def GetInvokePattern(self):
        if not self._invoke:
            return None
        return SimpleNamespace(Invoke=lambda: setattr(
            self, "invocations", self.invocations + 1))

    def GetTogglePattern(self):
        return None

    def GetValuePattern(self):
        if not self._editable:
            return None
        return SimpleNamespace(IsReadOnly=False, SetValue=lambda text: self.writes.append(text))

    def Exists(self, timeout):
        return True


def test_windows_uia_native_invoke_value_and_foreground_fencing(monkeypatch):
    root = UiaNode("WindowControl", "App")
    button = UiaNode("ButtonControl", "Go", invoke=True)
    edit = UiaNode("EditControl", "Search", editable=True)
    pwd = UiaNode("EditControl", "Password", password=True, editable=True)
    root.children = [button, edit, pwd]
    button.next, edit.next = edit, pwd
    driver = WindowsDesktopDriver(environ={})
    monkeypatch.setattr(driver, "_active_uia", lambda: (object(), 31, root))
    inspection = driver.inspect_elements()
    assert [(e.role, e.label) for e in inspection.elements] == [
        ("button", "Go"), ("edit", "Search"),
    ]
    driver.click_element(inspection, inspection.elements[0])
    driver.set_element_text(inspection, inspection.elements[1], "你好")
    assert button.invocations == 1
    assert edit.writes == ["你好"]
    monkeypatch.setattr(driver, "_active_uia", lambda: (object(), 32, root))
    with pytest.raises(ValueError, match="Foreground"):
        driver.click_element(inspection, inspection.elements[0])


class AtspiNode:
    def __init__(self, role, name, states=(), actions=()):
        self._role = role
        self.name = name
        self.states = set(states)
        self.actions = tuple(actions)
        self.children = []
        self.pressed = 0
        self.writes = []

    @property
    def childCount(self):
        return len(self.children)

    def getChildAtIndex(self, index):
        return self.children[index]

    def getState(self):
        return SimpleNamespace(contains=lambda state: state in self.states)

    def getRoleName(self):
        return self._role

    def queryAction(self):
        return SimpleNamespace(
            nActions=len(self.actions),
            getName=lambda index: self.actions[index],
            doAction=lambda index: self._do_action(index),
        )

    def _do_action(self, index):
        self.pressed += 1
        return True

    def queryEditableText(self):
        return SimpleNamespace(setTextContents=lambda text: self.writes.append(text))


def test_linux_atspi_native_press_edit_and_active_window_fencing(monkeypatch):
    atspi = SimpleNamespace(STATE_SENSITIVE="sensitive", STATE_EDITABLE="editable")
    window = AtspiNode("frame", "App")
    button = AtspiNode("push button", "Go", ("sensitive",), ("click",))
    edit = AtspiNode("entry", "Search", ("sensitive", "editable"))
    pwd = AtspiNode("password text", "Password", ("sensitive", "editable"))
    window.children = [button, edit, pwd]
    driver = LinuxX11DesktopDriver(environ={"DISPLAY": ":11"})
    monkeypatch.setattr(driver, "_active_atspi", lambda: (atspi, window))
    snapshot = driver.inspect_elements()
    assert [(e.role, e.label) for e in snapshot.elements] == [
        ("push button", "Go"), ("entry", "Search")
    ]
    driver.click_element(snapshot, snapshot.elements[0])
    driver.set_element_text(snapshot, snapshot.elements[1], "你好")
    assert button.pressed == 1
    assert edit.writes == ["你好"]
    monkeypatch.setattr(driver, "_active_atspi", lambda: (
        atspi, AtspiNode("frame", "Different window")))
    with pytest.raises(ValueError, match="Foreground"):
        driver.click_element(snapshot, snapshot.elements[0])


def test_accessibility_snapshot_is_bounded_and_redacted():
    from core.computer.accessibility import safe_label
    assert len(safe_label("A" * 300)) == 120
    assert safe_label("hello\n secret\tdata") == "hello secret data"
    secret = AccessibleElement(object(), "edit", "password: 123", True, True, True)
    public = secret.public("ref:1")
    assert public["label"] == "[protected]"
    assert public["actions"] == []
