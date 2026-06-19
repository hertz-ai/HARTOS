#!/usr/bin/env python3
# Minimal gtk4-layer-shell smoke client — NO HARTOS, NO WebKit, NO server.
# A solid-color GTK4 DrawingArea as a BACKGROUND layer surface. Isolates "does
# gtk4-layer-shell + LD_PRELOAD actually create a layer surface on HART-comp" from
# the heavy glass-shell stack. Writes is_layer_window to a status file.
import os
import time

STATUS = os.environ.get("HART_MIN_STATUS", "/tmp/m2-min-status.log")


def status(m):
    with open(STATUS, "a") as f:
        f.write(f"{time.strftime('%H:%M:%S')} {m}\n")
        f.flush()
        os.fsync(f.fileno())


import gi  # noqa: E402

gi.require_version("Gtk", "4.0")
gi.require_version("Gtk4LayerShell", "1.0")
from gi.repository import Gtk, Gdk, Gtk4LayerShell as LayerShell  # noqa: E402

status(f"gtk4-layer-shell version "
       f"{LayerShell.get_major_version()}.{LayerShell.get_minor_version()}."
       f"{LayerShell.get_micro_version()}")


def on_activate(app):
    win = Gtk.ApplicationWindow(application=app)
    LayerShell.init_for_window(win)
    LayerShell.set_layer(win, LayerShell.Layer.BACKGROUND)
    for e in (LayerShell.Edge.TOP, LayerShell.Edge.BOTTOM,
              LayerShell.Edge.LEFT, LayerShell.Edge.RIGHT):
        LayerShell.set_anchor(win, e, True)
    LayerShell.set_exclusive_zone(win, 0)

    # A solid magenta DrawingArea so the layer surface has visible content.
    area = Gtk.DrawingArea()

    def draw(_a, cr, w, h):
        cr.set_source_rgb(0.85, 0.10, 0.55)  # magenta
        cr.paint()

    area.set_draw_func(draw)
    win.set_child(area)
    win.present()
    is_layer = bool(LayerShell.is_layer_window(win))
    status(f"presented; is_layer_window={is_layer}")

    # self-screenshot the sway host then quit
    def shot():
        import subprocess
        out = os.environ.get("HART_SHOT_PATH", "/tmp/m2-min-shell.png")
        env = dict(os.environ)
        env["WAYLAND_DISPLAY"] = os.environ.get("HART_HOST_DISPLAY", "wayland-1")
        try:
            r = subprocess.run(["grim", out], env=env, capture_output=True,
                               text=True, timeout=15)
            status(f"grim rc={r.returncode} {out} err={r.stderr.strip()[:100]}")
        except Exception as e:
            status(f"grim FAILED {e!r}")
        app.quit()
        return False

    from gi.repository import GLib
    GLib.timeout_add(1500, shot)


app = Gtk.Application(application_id="ai.hart.MinLayer")
app.connect("activate", on_activate)
app.run(None)
