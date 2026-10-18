"""Render the Glowcast window offscreen in every mode, with sample lights glowing.

Run through dev/screenshots.sh. Uses throwaway settings and a fake bridge, so
your lights and settings are never touched. Also checks the spacing below the
status card, which a layout bug once broke.
"""

import pathlib
import sys
import tempfile
import time
import traceback
from types import SimpleNamespace

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Graphene", "1.0")
from gi.repository import Adw, GLib, Graphene, Gtk

OUT = pathlib.Path(sys.argv[1])
from glowcast.config import settings_manager as sm

tmp = pathlib.Path(tempfile.mkdtemp())
sm.SettingsManager._get_config_dir = lambda self: tmp
settings = sm.SettingsManager()
import glowcast.gui.main_window as mw

mw.TrayIcon = lambda *a: (_ for _ in ()).throw(RuntimeError("no tray"))
from glowcast.app_context import AppContext
from glowcast.colors import hex_to_xy, xy_to_rgb
from glowcast.mode_manager import Mode

LIGHTS = [
    (-0.74, 1.0, "#FF0000"),
    (-0.1, 1.0, "#FFD000"),
    (-0.04, -0.41, "#0050FF"),
    (0, 0, "#FF2000"),
    (0, 0, "#FFE000"),
    (0, 0, "#00C040"),
]
errors = []
sys.excepthook = lambda t, v, tb: errors.append("".join(traceback.format_exception(t, v, tb)))


def pump(seconds):
    end = time.monotonic() + seconds
    ctx = GLib.MainContext.default()
    while time.monotonic() < end:
        ctx.iteration(False)
        time.sleep(0.005)


def shot(win, name):
    w, h = win.get_width(), win.get_height()
    for _ in range(20):  # wait until GTK has drawn a frame we can copy
        image = Gtk.WidgetPaintable.new(win).get_current_image()
        snap = Gtk.Snapshot()
        image.snapshot(snap, w, h)
        node = snap.to_node()
        if node is not None:
            break
        win.queue_draw()
        pump(0.1)
    else:
        print("  (no frame for", name, ")")
        return
    win.get_renderer().render_texture(node, Graphene.Rect().init(0, 0, w, h)).save_to_png(
        str(OUT / f"{name}.png")
    )


def bottom(widget, win):
    _ok, b = widget.compute_bounds(win)
    return b.get_y() + b.get_height()


def top(widget, win):
    _ok, b = widget.compute_bounds(win)
    return b.get_y()


def run(app):
    try:
        ctx = AppContext(settings)
        ctx.light_glow = lambda: [(x, z, xy_to_rgb(*hex_to_xy(c)), 0.9) for x, z, c in LIGHTS]
        # Look like a running sync: a frame rate and moving meters
        from glowcast.music import AudioFeatures

        ctx.sync_controller.get_stats = lambda: {"fps": 50.0}
        ctx.music_controller.get_stats = lambda: {"fps": 50.0}
        ctx.music_controller.latest_features = AudioFeatures(0.8, 0.55, 0.35, 0.0, 0.0, 0.0, False)
        mm = ctx.mode_manager

        def go(mode):
            mm._set_mode(mode)
            return True

        mm.switch_to_screen = go
        mm.switch_to_music = lambda: go(Mode.MUSIC)
        mm.switch_to_scene = lambda scene, b, d, superseded=None: go(Mode.SCENES)
        mm.turn_off = lambda lights_off=False: go(Mode.OFF)
        app.shortcuts = None
        win = mw.MainWindow(app, ctx)
        win.set_default_size(930, 1000)
        win.present()
        pump(1.0)
        win._on_bridge_status_received(
            SimpleNamespace(
                connected=True,
                configured=True,
                bridge_ip="x",
                entertainment_zone_name="Living room",
                entertainment_channel_count=6,
            )
        )
        for mode in (Mode.MUSIC, Mode.SCENES, Mode.VIDEO, Mode.GAMES):
            win.mode_cards[mode].set_active(True)
            if not win._sync_active:
                win.toggle_sync()
            win._worker.wait_idle(2)
            pump(2.0)
            win.queue_draw()
            pump(0.3)
            row = win.mode_cards[mode].get_parent()
            gap = top(row, win) - bottom(win.status_card, win)
            card_h = win.status_card.get_height()
            print(f"{mode.name:6} status card height={card_h} gap to mode buttons={gap:.0f}px")
            shot(win, mode.name.lower())
    except Exception:
        errors.append(traceback.format_exc())
    print("ERRORS:", len(errors))
    [print(e) for e in errors]
    app.quit()


app = Adw.Application(application_id="io.github.stensel8.glowcast.shot")
app.connect("activate", run)
app.run(None)
