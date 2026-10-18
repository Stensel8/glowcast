"""Glowcast application entry point."""

import os
import signal

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, Gio, GLib, Gtk

from glowcast.app_context import AppContext
from glowcast.config.settings_manager import SettingsManager
from glowcast.gui.main_window import MainWindow
from glowcast.mode_manager import Mode
from glowcast.shortcuts import GlobalShortcuts
from glowcast.utils.logging import timed_print

# The app icon in a checkout; the Flatpak installs it into /app/share/icons instead.
APP_ICON_DIR = os.path.normpath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "flatpak")
)
LAUNCH_ATTEMPTS = 30  # every 2 s: the network may still be coming up after login


class GlowcastApp(Adw.Application):
    def __init__(self):
        super().__init__(application_id="io.github.stensel8.glowcast", flags=Gio.ApplicationFlags.FLAGS_NONE)
        self.connect("activate", self.on_activate)
        self.main_window = None
        self.app_context = None
        self.shortcuts = None
        self._launch_attempts = 0
        self._setup_actions()
        # Stop syncing cleanly on SIGTERM/SIGINT (logout, Ctrl+C)
        signal.signal(signal.SIGTERM, self._handle_signal)
        signal.signal(signal.SIGINT, self._handle_signal)

    def _setup_actions(self):
        """Actions for the tray menu and the keyboard shortcuts."""
        window = lambda: self.main_window  # noqa: E731 - created later
        actions = {
            "show": lambda: window().present(),
            "quit": self._quit,
            "toggle-sync": lambda: window().toggle_sync(),
            "toggle-video": lambda: window().toggle_mode(Mode.VIDEO),
            "toggle-music": lambda: window().toggle_mode(Mode.MUSIC),
            "toggle-games": lambda: window().toggle_mode(Mode.GAMES),
            "brightness-up": lambda: window().change_brightness(1),
            "brightness-down": lambda: window().change_brightness(-1),
            "toggle-lights": lambda: window().toggle_lights(),
        }
        for name, callback in actions.items():
            action = Gio.SimpleAction.new(name, None)
            action.connect("activate", lambda _a, _p, cb=callback: self.main_window and cb())
            self.add_action(action)

    def _handle_signal(self, signum, _frame):
        timed_print(f"Received signal {signum}, shutting down...")
        GLib.idle_add(self._quit)

    def _quit(self):
        if self.main_window:
            self.main_window._quitting = True
        self.quit()
        return GLib.SOURCE_REMOVE

    def on_activate(self, _app):
        if self.main_window:
            self.main_window.present()
            return

        Adw.StyleManager.get_default().set_color_scheme(Adw.ColorScheme.PREFER_DARK)
        Gtk.IconTheme.get_for_display(Gdk.Display.get_default()).add_search_path(APP_ICON_DIR)

        settings = SettingsManager.get_instance()
        self.app_context = AppContext(settings)
        status = self.app_context.get_bridge_status()
        if status.connected:
            timed_print(f"Connected to Hue bridge at {status.bridge_ip}")
        else:
            timed_print("Warning: Could not connect to Hue bridge; set it up in Settings")

        self.main_window = MainWindow(self, self.app_context)
        if settings.ui.minimize_at_startup and self.main_window._tray_icon:
            self.main_window.hide()
        else:
            self.main_window.present()

        self.shortcuts = GlobalShortcuts(lambda shortcut_id: self.activate_action(shortcut_id, None))
        if settings.ui.shortcuts:
            # Re-registers the keys chosen before; the desktop remembers them.
            GLib.Thread.new("shortcuts", self.shortcuts.bind)

        if settings.ui.start_sync_at_launch or settings.ui.lights_on_at_launch:
            GLib.timeout_add_seconds(2, self._start_at_launch)

    def _start_at_launch(self):
        self._launch_attempts += 1
        if self.main_window.start_at_launch():
            return GLib.SOURCE_REMOVE
        if self._launch_attempts >= LAUNCH_ATTEMPTS:
            timed_print("Bridge not reachable; not starting at launch")
            return GLib.SOURCE_REMOVE
        self.main_window._check_bridge_connection()
        return GLib.SOURCE_CONTINUE

    def do_shutdown(self):
        timed_print("Shutting down...")
        if self.main_window:
            self.main_window._save_brightness.flush()
        if self.app_context:
            self.app_context.shutdown()
        Adw.Application.do_shutdown(self)


def main():
    GlowcastApp().run(None)


if __name__ == "__main__":
    main()
