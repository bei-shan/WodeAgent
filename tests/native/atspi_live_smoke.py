"""Disposable real Linux AT-SPI + GTK smoke: run ONLY inside CI Xvfb+D-Bus.

OS packages: python3-pyatspi, python3-gi, gir1.2-gtk-3.0, at-spi2-core,
openbox. Intended for /usr/bin/python3 where distro GI bindings are installed.
"""
from __future__ import annotations

import os
import subprocess
import sys
import time


def app_main():
    import gi
    gi.require_version("Gtk", "3.0")
    from gi.repository import Gtk, GLib

    window = Gtk.Window(title="Ridge Native AT-SPI Smoke")
    window.set_default_size(400, 240)
    window.connect("destroy", Gtk.main_quit)
    layout = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
    entry = Gtk.Entry()
    entry.set_placeholder_text("Search")
    entry.set_name("ridge-smoke-input")
    button = Gtk.Button(label="Go")
    button.set_name("ridge-smoke-button")
    layout.pack_start(entry, True, True, 0)
    layout.pack_start(button, True, True, 0)
    window.add(layout)
    window.show_all()
    window.present()
    GLib.timeout_add_seconds(18, Gtk.main_quit)
    Gtk.main()


def smoke_main():
    from core.computer.platforms.linux import LinuxX11DesktopDriver

    if not os.environ.get("DISPLAY") or "DBUS_SESSION_BUS_ADDRESS" not in os.environ:
        raise RuntimeError("Needs disposable X11 + session D-Bus")
    env = dict(os.environ)
    env["NO_AT_BRIDGE"] = "0"
    # Minimal WM ensures the GTK window becomes an active X11 window.
    wm = subprocess.Popen(
        ["openbox"], env=env, stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    app = None
    try:
        time.sleep(1)
        app = subprocess.Popen(
            [sys.executable, __file__, "--app"], env=env,
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
        )
        driver = LinuxX11DesktopDriver(environ=env)
        inspection = None
        last_error = None
        for _ in range(35):
            if app.poll() is not None:
                raise RuntimeError("Test GTK application exited before accessible inspection")
            try:
                candidate = driver.inspect_elements()
                labels = [e.label for e in candidate.elements]
                if "Go" in labels and any(e.can_set_text for e in candidate.elements):
                    inspection = candidate
                    break
            except Exception as exc:
                last_error = type(exc).__name__
            time.sleep(0.3)
        if inspection is None:
            raise RuntimeError(
                "AT-SPI did not expose GTK controls (last provider error: "
                + str(last_error) + ")"
            )
        button = next(e for e in inspection.elements if e.label == "Go")
        edit = next(e for e in inspection.elements if e.can_set_text)
        driver.set_element_text(inspection, edit, "native atspi text")
        driver.click_element(inspection, button)
        print("PASS: real GTK AT-SPI inspect/edit/click")
    finally:
        if app is not None:
            app.terminate()
            try:
                app.communicate(timeout=3)
            except subprocess.TimeoutExpired:
                app.kill()
                app.communicate()
        wm.terminate()
        try:
            wm.wait(timeout=3)
        except subprocess.TimeoutExpired:
            wm.kill()
            wm.wait()


if __name__ == "__main__":
    if "--app" in sys.argv:
        app_main()
    else:
        smoke_main()
