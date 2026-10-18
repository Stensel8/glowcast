"""Main window: status, the four Hue Sync modes, the active mode's panel and the sync button.

While syncing, picking another mode switches over immediately. Slow bridge
work runs on a background worker so the window never freezes.
"""

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, Gio, GLib, Gtk

from glowcast import __version__
from glowcast.app_context import AppContext
from glowcast.gui.glow import GlowCard, WindowGlow
from glowcast.gui.music_panel import MusicPanel
from glowcast.gui.scenes_panel import ScenesPanel
from glowcast.gui.settings_dialog import SettingsDialog
from glowcast.gui.tray_icon import TrayIcon
from glowcast.gui.video_panel import VideoPanel
from glowcast.gui.widgets import DebouncedSave
from glowcast.mode_manager import SCREEN_MODES, STREAMING_MODES, Mode
from glowcast.presets import DEFAULT_SCENE, SCENE_PRESETS
from glowcast.utils.logging import timed_print
from glowcast.worker import Worker

MODES = (  # Hue Sync's order
    (Mode.SCENES, "Scenes", "weather-clear-night-symbolic"),
    (Mode.GAMES, "Games", "input-gaming-symbolic"),
    (Mode.MUSIC, "Music", "audio-x-generic-symbolic"),
    (Mode.VIDEO, "Video", "video-display-symbolic"),
)
STATUS = {
    Mode.VIDEO: ("Video Sync", "Lights follow your screen"),
    Mode.GAMES: ("Game Sync", "Lights follow your game"),
    Mode.MUSIC: ("Music Sync", "Lights follow your desktop audio"),
}
BRIGHTNESS_STEP_PERCENT = 10

CSS = """
.status-card { padding: 16px 20px; border-radius: 12px; }
.status-connected { background: alpha(@success_color, 0.1); border: 1px solid alpha(@success_color, 0.3); }
.status-disconnected { background: alpha(@warning_color, 0.1); border: 1px solid alpha(@warning_color, 0.3); }
/* see-through while syncing: the live gradient of the lights shows instead */
.status-syncing { background: alpha(@window_fg_color, 0.04); border: 1px solid alpha(@accent_color, 0.4); }
.status-scene { background: alpha(@window_fg_color, 0.04); border: 1px solid alpha(@yellow_3, 0.4); }
.stats-label { font-size: 12px; font-weight: 600; letter-spacing: 0.5px; opacity: 0.7; }
.stats-value { font-size: 24px; font-weight: 700; font-feature-settings: "tnum"; }
.mode-card { padding: 12px 8px; border-radius: 14px; min-width: 96px; }
.mode-card:checked { background: alpha(@accent_bg_color, 0.2); box-shadow: inset 0 0 0 2px @accent_bg_color; }
.preview-card { padding: 8px; }
.palette-card, .meter-card { padding: 12px 16px; }
.palette-tile { padding: 4px; border-radius: 12px; }
.palette-active { box-shadow: 0 0 0 3px @accent_color; }
.new-tile { border: 2px dashed alpha(@window_fg_color, 0.3); border-radius: 8px; }
.scene-card { padding: 8px 4px; border-radius: 14px; }
.scene-card.palette-active { box-shadow: none; background: alpha(@accent_bg_color, 0.25); }
.new-scene { border: 2px dashed alpha(@window_fg_color, 0.3); border-radius: 999px; }
.sync-button { padding: 12px 36px; font-weight: bold; }
"""


class MainWindow(Adw.ApplicationWindow):
    __gtype_name__ = "GlowcastMainWindow"

    def __init__(self, app, app_context: AppContext):
        super().__init__(application=app)
        self.app_context = app_context
        self.settings = app_context.settings
        self.mode_manager = app_context.mode_manager
        self.sync_controller = app_context.sync_controller
        self.music_controller = app_context.music_controller
        self.bridge_connected = False
        self._selected_mode = Mode(self.settings.ui.last_mode)
        # What the user asked for. A switch to a scene takes ~1 s, during which
        # the mode manager reports OFF; clicks then must still switch live.
        self._sync_active = False
        self._active_mode = Mode.OFF
        self._area_name = ""
        self._zone_summary = ""
        self._updating_cards = False
        self._tray_icon = None
        self._quitting = False
        self._told_about_tray = False
        self._worker = Worker()
        # One brightness for every mode, like Hue Sync's single slider: all
        # panels' sliders share this adjustment, so they move together.
        self.brightness = Gtk.Adjustment(
            value=round(self.settings.lights.brightness * 100), lower=1, upper=100, step_increment=1
        )
        self.brightness.connect("value-changed", self._on_brightness_changed)
        self._save_brightness = DebouncedSave(self.settings)
        self._scene_apply_source = 0

        self.set_title("Glowcast")
        self.set_default_size(720, 820)
        css = Gtk.CssProvider()
        css.load_from_string(CSS)
        Gtk.StyleContext.add_provider_for_display(
            Gdk.Display.get_default(), css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
        )
        self._build_ui()

        self.mode_manager.set_mode_changed_callback(self._on_mode_changed_threadsafe)
        self._setup_tray_icon()
        self._minimized_by_us = False
        self.connect("notify::minimized", self._on_window_minimized)

        self._check_bridge_connection()
        self.status_timeout_id = GLib.timeout_add(100, self._update_status)

    # --- layout -------------------------------------------------------------

    def _build_ui(self):
        header = Adw.HeaderBar(centering_policy=Adw.CenteringPolicy.STRICT)
        for icon, tooltip, handler in (
            ("emblem-system-symbolic", "Settings", self._on_settings_clicked),
            ("help-about-symbolic", "About Glowcast", self._on_about_clicked),
        ):
            button = Gtk.Button(icon_name=icon, tooltip_text=tooltip)
            button.add_css_class("flat")
            button.connect("clicked", handler)
            header.pack_end(button)

        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=24)
        for side in ("top", "bottom", "start", "end"):
            getattr(content, f"set_margin_{side}")(24)
        content.append(self._build_status_card())
        content.append(self._build_mode_cards())

        self.panel_stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE, vhomogeneous=False)
        self._build_panels()
        content.append(self.panel_stack)

        self.sync_button = Gtk.Button(halign=Gtk.Align.CENTER)
        self.sync_button.add_css_class("pill")
        self.sync_button.add_css_class("sync-button")
        self.sync_button.connect("clicked", lambda _b: self.toggle_sync())
        content.append(self.sync_button)
        self._update_sync_button()

        clamp = Adw.Clamp(maximum_size=760, tightening_threshold=600, child=content)
        scrolled = Gtk.ScrolledWindow(
            hscrollbar_policy=Gtk.PolicyType.NEVER, child=clamp, propagate_natural_height=True
        )
        # The lights glow behind the window, as in Hue Sync
        glowing = Gtk.Overlay(child=WindowGlow(self.app_context.light_glow))
        glowing.add_overlay(scrolled)
        toolbar_view = Adw.ToolbarView(content=glowing)
        toolbar_view.add_top_bar(header)
        self.set_content(toolbar_view)

    def _build_panels(self):
        self.panels = {
            Mode.SCENES: ScenesPanel(self.app_context, self._on_scene_changed, self.brightness),
            Mode.GAMES: VideoPanel(self.app_context, "games", self._on_capture_area_changed, self.brightness),
            Mode.MUSIC: MusicPanel(self.app_context, self.brightness),
            Mode.VIDEO: VideoPanel(self.app_context, "video", self._on_capture_area_changed, self.brightness),
        }
        for mode, panel in self.panels.items():
            self.panel_stack.add_named(panel, mode.value)
        self.panel_stack.set_visible_child_name(self._selected_mode.value)

    def _rebuild_panels(self):
        """Recreate the panels so they show freshly reset settings."""
        self.panels[Mode.MUSIC].set_running(False)
        for panel in self.panels.values():
            self.panel_stack.remove(panel)
        self.brightness.set_value(round(self.settings.lights.brightness * 100))
        self._build_panels()
        self.panels[Mode.MUSIC].set_running(self.mode_manager.current_mode == Mode.MUSIC)

    def _build_status_card(self) -> Gtk.Widget:
        # Tinted with the lights' colours while syncing, like Hue Sync's top card
        self.status_card = GlowCard(self.app_context.light_glow, spacing=12)
        self.status_card.add_css_class("status-card")

        text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, hexpand=True)
        self.status_label = Gtk.Label(label="Searching for bridge…", xalign=0)
        self.status_label.add_css_class("title-3")
        self.status_subtitle = Gtk.Label(xalign=0, wrap=True)  # errors can be long
        self.status_subtitle.add_css_class("dim-label")
        text.append(self.status_label)
        text.append(self.status_subtitle)
        self.status_card.append(text)

        self.fps_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, valign=Gtk.Align.CENTER, visible=False)
        fps_label = Gtk.Label(label="FPS")
        fps_label.add_css_class("stats-label")
        self.fps_value = Gtk.Label(label="0")
        self.fps_value.add_css_class("stats-value")
        self.fps_box.append(fps_label)
        self.fps_box.append(self.fps_value)
        self.status_card.append(self.fps_box)

        self.retry_button = Gtk.Button(label="Retry Connection", valign=Gtk.Align.CENTER, visible=False)
        self.retry_button.connect("clicked", lambda _b: self._check_bridge_connection())
        self.status_card.append(self.retry_button)
        self.open_bridge_settings_btn = Gtk.Button(label="Settings", valign=Gtk.Align.CENTER, visible=False)
        self.open_bridge_settings_btn.add_css_class("flat")
        self.open_bridge_settings_btn.connect("clicked", self._on_settings_clicked)
        self.status_card.append(self.open_bridge_settings_btn)
        return self.status_card

    def _build_mode_cards(self) -> Gtk.Widget:
        row = Gtk.Box(spacing=12, halign=Gtk.Align.CENTER)
        self.mode_cards = {}
        group = None
        for mode, label, icon in MODES:
            card_content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
            card_content.append(Gtk.Image(icon_name=icon, pixel_size=32))
            card_content.append(Gtk.Label(label=label))
            card = Gtk.ToggleButton(child=card_content)
            card.set_group(group)
            card.add_css_class("flat")
            card.add_css_class("mode-card")
            card.connect("toggled", self._on_mode_card_toggled, mode)
            row.append(card)
            self.mode_cards[mode] = card
            group = group or card
        self._updating_cards = True
        self.mode_cards[self._selected_mode].set_active(True)
        self._updating_cards = False
        return row

    # --- modes and sync -----------------------------------------------------

    def _select_mode_card(self, mode: Mode) -> None:
        """Show a mode's card and panel without treating it as a user click."""
        self._selected_mode = mode
        self._updating_cards = True
        self.mode_cards[mode].set_active(True)
        self._updating_cards = False
        self.panel_stack.set_visible_child_name(mode.value)

    def _on_mode_card_toggled(self, card, mode: Mode) -> None:
        if not card.get_active() or self._updating_cards:
            return
        self._select_mode_card(mode)
        if self._sync_active and self._active_mode != mode:
            self._start_mode(mode)  # live switch while syncing

    def toggle_sync(self) -> None:
        """Start light sync in the selected mode, or stop it."""
        if self._sync_active:
            self._stop_sync()
        else:
            self._start_mode(self._selected_mode)

    def toggle_mode(self, mode: Mode) -> None:
        """Hue Sync's "Toggle Video/Music/Game Sync" shortcuts."""
        if self._sync_active and self._active_mode == mode:
            self._stop_sync()
        else:
            self._start_mode(mode)

    def _start_mode(self, mode: Mode) -> None:
        """Show the new mode at once; the switch itself runs on the worker."""
        if not self.bridge_connected:
            self._set_status("Cannot start", "Disconnected from bridge", "disconnected")
            return
        self._sync_active = True
        self._active_mode = mode
        self._select_mode_card(mode)
        self.settings.ui.last_mode = mode.value
        self.settings.save()
        self._set_status(f"Switching to {mode.value.title()}…", "", "syncing")
        self._update_sync_button()
        self._worker.submit("mode", lambda: self._switch(mode))
        if mode in STREAMING_MODES and self._should_minimize_to_tray():
            self.hide()

    def _switch(self, mode: Mode, scene_key: str | None = None) -> None:
        """Worker thread: perform a mode switch."""
        if mode in SCREEN_MODES:
            started = self.mode_manager.switch_to_screen(mode)
        elif mode == Mode.MUSIC:
            started = self.mode_manager.switch_to_music()
        else:
            scenes = self.settings.scenes
            scene = self.app_context.scenes.scene(scene_key or scenes.scene, scenes.custom)
            started = self.mode_manager.switch_to_scene(
                scene or SCENE_PRESETS[DEFAULT_SCENE],
                round(254 * self.settings.lights.brightness),
                scenes.dynamic,
                superseded=lambda: self._worker.is_pending("mode"),
            )
        if not started and not self._worker.is_pending("mode"):
            if self.mode_manager.in_use_by_other:
                GLib.idle_add(self._ask_take_over, mode)
            else:
                GLib.idle_add(self._on_switch_failed, self.mode_manager.last_error)

    def _ask_take_over(self, mode: Mode) -> bool:
        """Another app streams to the area: offer Hue Sync's "Start anyway"."""
        self._on_switch_failed("Another app is syncing this entertainment area")
        alert = Adw.AlertDialog(
            heading="Area in use",
            body="Lights will stop syncing with the other app if you start light sync.",
        )
        alert.add_response("cancel", "Cancel")
        alert.add_response("start", "Start anyway")
        alert.set_response_appearance("start", Adw.ResponseAppearance.SUGGESTED)

        def respond(_dialog, response):
            if response == "start":
                self.mode_manager.take_over = True
                self._start_mode(mode)

        alert.connect("response", respond)
        alert.present(self)
        return GLib.SOURCE_REMOVE

    def _on_switch_failed(self, error: str) -> bool:
        self._sync_active = False
        self._active_mode = Mode.OFF
        self._set_status("Connection failed", error or "Check the entertainment area", "disconnected")
        self.fps_box.set_visible(False)
        self._update_sync_button()
        return GLib.SOURCE_REMOVE

    def _stop_sync(self, lights_off: bool = False) -> None:
        """Stop syncing; the bridge returns the lights to how they were."""
        self._sync_active = False
        self._active_mode = Mode.OFF
        self._update_sync_button()
        self._worker.submit("mode", lambda: self.mode_manager.turn_off(lights_off=lights_off))
        if self._should_minimize_to_tray():
            self.present()

    def _on_scene_changed(self) -> None:
        """Picking a scene (or its brightness/Dynamics) applies it right away."""
        self._start_mode(Mode.SCENES)

    def _on_capture_area_changed(self) -> None:
        for mode in SCREEN_MODES:
            self.panels[mode].show_capture_area()

    def _on_mode_changed_threadsafe(self, mode: Mode) -> None:
        """Mode manager callback; may come from the worker thread."""
        GLib.idle_add(self._on_mode_changed, mode)

    def _on_mode_changed(self, mode: Mode) -> bool:
        """Reflect the active mode everywhere (GTK thread)."""
        if self._worker.is_pending("mode"):
            return GLib.SOURCE_REMOVE  # a newer switch is on its way; keep showing that
        was_active = self._sync_active
        self._sync_active = mode != Mode.OFF
        self._active_mode = mode
        if mode != Mode.OFF:
            self._select_mode_card(mode)
        if mode in STATUS:
            self._set_status(*STATUS[mode], "syncing")
        elif mode == Mode.SCENES:
            scenes = self.settings.scenes
            scene = self.app_context.scenes.scene(scenes.scene, scenes.custom)
            self._set_status(scene.name if scene else "Scene", self._area_name, "scene")
        else:
            self._set_status("Ready", self._zone_summary, "connected")
            for screen_mode in SCREEN_MODES:
                self.panels[screen_mode].clear_preview()
        self.fps_box.set_visible(mode in STREAMING_MODES)
        self.panels[Mode.MUSIC].set_running(mode == Mode.MUSIC)
        self._update_sync_button()
        if was_active != self._sync_active:
            self._notify(
                "Light sync started" if self._sync_active else "Light sync stopped",
                f"{self._area_name or 'Your entertainment area'} is syncing with {mode.value}"
                if self._sync_active
                else "",
            )
        return GLib.SOURCE_REMOVE

    def start_at_launch(self) -> bool:
        """General preferences: start syncing, or turn on a scene, at launch.

        False until the bridge is reachable (right after login the network
        may still be coming up).
        """
        if not self.bridge_connected:
            return False
        ui = self.settings.ui
        if ui.start_sync_at_launch:
            self._start_mode(Mode(ui.last_mode if ui.default_mode == "last" else ui.default_mode))
        elif ui.lights_on_at_launch:
            self._sync_active, self._active_mode = True, Mode.SCENES
            self._select_mode_card(Mode.SCENES)
            self._update_sync_button()
            self._worker.submit("mode", lambda: self._switch(Mode.SCENES, ui.launch_scene))
        return True

    # --- shortcut actions -----------------------------------------------------

    def change_brightness(self, direction: int) -> None:
        """Increase/decrease brightness shortcuts."""
        self.brightness.set_value(self.brightness.get_value() + direction * BRIGHTNESS_STEP_PERCENT)

    def _on_brightness_changed(self, adjustment) -> None:
        """Video, games and music read the brightness live; a scene is set again."""
        self.settings.lights.brightness = adjustment.get_value() / 100
        self._save_brightness()
        if self._active_mode == Mode.SCENES:
            # Once the slider rests: each step is a request per light, and the
            # bridge throttles bursts.
            if self._scene_apply_source:
                GLib.source_remove(self._scene_apply_source)
            self._scene_apply_source = GLib.timeout_add(200, self._apply_scene_brightness)

    def _apply_scene_brightness(self) -> bool:
        self._scene_apply_source = 0
        if self._active_mode == Mode.SCENES:
            self._start_mode(Mode.SCENES)
        return GLib.SOURCE_REMOVE

    def toggle_lights(self) -> None:
        """Turn lights on / off shortcut: the entertainment area's lights."""
        if self._sync_active:
            self._stop_sync(lights_off=True)
            return
        scenes = self.app_context.scenes
        self._worker.submit("lights", lambda: scenes.set_lights_on(not scenes.any_light_on()))

    # --- status ---------------------------------------------------------------

    def _update_sync_button(self) -> None:
        active = self._sync_active
        self.sync_button.set_label("Stop light sync" if active else "Start light sync")
        self.sync_button.remove_css_class("suggested-action" if active else "destructive-action")
        self.sync_button.add_css_class("destructive-action" if active else "suggested-action")
        if self._tray_icon:
            self._tray_icon.update_sync_status(active)

    def _set_status(self, title: str, subtitle: str, state: str) -> None:
        self.status_label.set_text(title)
        self.status_subtitle.set_text(subtitle)
        self.status_subtitle.set_visible(bool(subtitle))
        for name in ("connected", "disconnected", "syncing", "scene"):
            self.status_card.remove_css_class(f"status-{name}")
        self.status_card.add_css_class(f"status-{state}")

    def _notify(self, title: str, body: str) -> None:
        """Desktop notification, when enabled and the window isn't in front."""
        if not self.settings.ui.notifications or self.is_active():
            return
        notification = Gio.Notification.new(title)
        if body:
            notification.set_body(body)
        self.get_application().send_notification("glowcast-sync", notification)

    def _update_status(self) -> bool:
        """Poll the video and music loops (every 100 ms on the GTK thread)."""
        mode = self.mode_manager.current_mode

        latest = _drain(self.sync_controller)
        if latest:
            kind, message, data = latest
            if kind == "status" and message == "syncing" and mode in SCREEN_MODES and data:
                self.panels[mode].update_preview(data)
            elif kind == "error":
                self._set_status("Error", message, "disconnected")

        latest = _drain(self.music_controller)
        if latest and latest[0] == "error":
            self._stop_sync()
            self._set_status("Error", latest[1], "disconnected")

        stream = self.app_context.entertainment_stream
        if self._sync_active and mode in STREAMING_MODES and stream and stream.taken_over:
            # The Hue app (or another app) took the lights: hand them over, like Hue Sync
            self._stop_sync()
            self._set_status("Light sync stopped", "Another app took over the lights", "connected")
            self._notify("Light sync stopped", "Another app took over your lights")
            return GLib.SOURCE_CONTINUE

        if mode in STREAMING_MODES:
            controller = self.music_controller if mode == Mode.MUSIC else self.sync_controller
            self.fps_value.set_text(f"{controller.get_stats()['fps']:.1f}")
        return GLib.SOURCE_CONTINUE

    # --- bridge -------------------------------------------------------------

    def _check_bridge_connection(self):
        self.retry_button.set_visible(False)
        self.app_context.get_bridge_status_async(self._on_bridge_status_received)

    def _on_bridge_status_received(self, status):
        newly_connected = status.connected and not self.bridge_connected
        self.bridge_connected = status.connected
        if status.connected:
            self._area_name = status.entertainment_zone_name
            if status.entertainment_zone_name:
                lights = status.entertainment_channel_count
                self._zone_summary = f"{self._area_name} · {lights} light{'s' if lights != 1 else ''}"
            else:
                self._zone_summary = "No entertainment area"
            if self.mode_manager.current_mode == Mode.OFF and not self._sync_active:
                self._set_status("Connected to bridge", self._zone_summary, "connected")
            if newly_connected:
                self.panels[Mode.SCENES].load_bridge_scenes()
        elif not status.configured:
            self._set_status("Not set up", "Open Settings to connect your Hue Bridge", "disconnected")
        else:
            self._set_status(
                "Disconnected from bridge", f"Cannot reach the bridge at {status.bridge_ip}", "disconnected"
            )
        for widget in (self.sync_button, *self.mode_cards.values()):
            widget.set_sensitive(status.connected)
        self.retry_button.set_visible(status.configured and not status.connected)
        self.open_bridge_settings_btn.set_visible(not status.configured)

    # --- dialogs, tray and window -------------------------------------------

    def _on_settings_clicked(self, _button=None):
        dialog = SettingsDialog(self, self.app_context)
        dialog.connect("closed", self._on_settings_closed)
        dialog.present(self)

    def _on_settings_closed(self, dialog=None):
        area_before = self.app_context.scenes.config_id
        self.app_context.apply_settings()
        if dialog is not None and dialog.was_reset:
            self._rebuild_panels()
        elif self.app_context.scenes.config_id != area_before:
            self.panels[Mode.SCENES].load_bridge_scenes()
        self._on_capture_area_changed()
        self._check_bridge_connection()
        self._on_mode_changed(self.mode_manager.current_mode)

    def _on_about_clicked(self, _button):
        about = Adw.AboutDialog(
            application_name="Glowcast",
            application_icon="io.github.stensel8.glowcast",
            developer_name="Sten T.",
            version=__version__,
            comments=(
                "Sync your Philips Hue lights with your screen, games and music, "
                "or set a scene, through your Hue entertainment area. Nothing you "
                "watch or hear leaves your computer.\n\n"
                "Not affiliated with Signify or Philips Hue."
            ),
            license_type=Gtk.License.MIT_X11,
            website="https://github.com/Stensel8/glowcast",
            issue_url="https://github.com/Stensel8/glowcast/issues",
        )
        about.add_credit_section("Based on", ["Lumux by Engin Kırmacı https://github.com/enginkirmaci/lumux"])
        about.present(self)

    def _setup_tray_icon(self):
        try:
            self._tray_icon = TrayIcon(self.get_application(), self)
            if not self._tray_icon.is_available:
                self._tray_icon = None
        except Exception as e:
            timed_print(f"Note: System tray not available: {e}")
            self._tray_icon = None

    def _should_minimize_to_tray(self) -> bool:
        return bool(self._tray_icon and self.settings.ui.minimize_to_tray_on_sync)

    def _on_window_minimized(self, window, _pspec):
        """Hide to the tray instead of minimizing, when a tray is available."""
        if window.props.minimized and not self._minimized_by_us and self._tray_icon:
            self._minimized_by_us = True
            self.hide()
            GLib.idle_add(self._reset_minimized_flag)

    def _reset_minimized_flag(self):
        self._minimized_by_us = False
        return GLib.SOURCE_REMOVE

    def do_close_request(self) -> bool:
        """Hide to the tray (Hue Sync's default), or quit if the user prefers that."""
        if self._tray_icon and not self._quitting and not self.settings.ui.close_quits:
            self.hide()
            if not self._told_about_tray:
                self._told_about_tray = True
                notification = Gio.Notification.new("Glowcast keeps running")
                notification.set_body("To quit, choose Quit in the tray menu.")
                self.get_application().send_notification("glowcast-tray", notification)
            return True

        self._worker.wait_idle(timeout=5)
        if self.status_timeout_id:
            GLib.source_remove(self.status_timeout_id)
            self.status_timeout_id = None
        self._save_brightness.flush()
        if self._tray_icon:
            self._tray_icon.destroy()
            self._tray_icon = None
        self.get_application().quit()  # do_shutdown stops syncing
        return False


def _drain(controller):
    """Newest queued status of a sync loop, discarding older ones."""
    latest = None
    while (status := controller.get_status()) is not None:
        latest = status
    return latest
