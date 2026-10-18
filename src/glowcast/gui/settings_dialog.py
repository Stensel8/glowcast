"""Settings, in Hue Sync's sections: Hue Bridge, General, Display, Audio, Keyboard shortcuts.

Everything applies right away, except the bridge connection, which applies
when the window closes.
"""

import threading

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib, Gtk

from glowcast.app_context import AppContext
from glowcast.audio import list_sources
from glowcast.autostart import set_autostart
from glowcast.capture import ScreenCapture
from glowcast.config.settings_manager import MAX_SYNC_DELAY_MS
from glowcast.gui.bridge_wizard import BridgeWizard
from glowcast.gui.widgets import switch_row
from glowcast.presets import SCENE_PRESETS
from glowcast.shortcuts import SHORTCUTS

DEFAULT_MODES = (("last", "Last used"), ("games", "Games"), ("music", "Music"), ("video", "Video"))
LIGHT_CHANNELS = ["Match number of lights"] + [str(n) for n in range(1, 8)]


def _combo_row(title: str, subtitle: str, labels: list[str], selected: int, on_changed) -> Adw.ComboRow:
    row = Adw.ComboRow(title=title, subtitle=subtitle, model=Gtk.StringList.new(labels))
    row.set_selected(max(0, selected))
    row.connect("notify::selected", lambda r, _p: on_changed(r.get_selected()))
    return row


class SettingsDialog(Adw.PreferencesDialog):
    def __init__(self, parent, app_context: AppContext):
        super().__init__(title="Settings")
        self.app_context = app_context
        self.settings = app_context.settings
        self.bridge = app_context.bridge
        self._shortcuts = getattr(parent.get_application(), "shortcuts", None)
        self._entertainment_configs = []
        self.was_reset = False
        for page in (
            self._build_bridge_page(),
            self._build_general_page(),
            self._build_display_page(),
            self._build_audio_page(),
            self._build_shortcuts_page(),
        ):
            self.add(page)
        self.connect("closed", self._on_closed)
        self._refresh_bridge_status()

    def _save(self, section, name: str):
        """Handler that stores a value in a settings section and saves."""

        def store(value):
            setattr(section, name, value)
            self.settings.save()

        return store

    # --- Hue Bridge -----------------------------------------------------------

    def _build_bridge_page(self) -> Adw.PreferencesPage:
        page = Adw.PreferencesPage(title="Hue Bridge", icon_name="network-server-symbolic")

        bridge_group = Adw.PreferencesGroup(title="Hue Bridge")
        self.status_row = Adw.ActionRow(title="Status", subtitle="Checking…")
        setup_button = Gtk.Button(label="Connect New Bridge", valign=Gtk.Align.CENTER)
        setup_button.add_css_class("suggested-action")
        setup_button.connect("clicked", self._on_start_wizard)
        self.status_row.add_suffix(setup_button)
        bridge_group.add(self.status_row)

        manual = Adw.ExpanderRow(title="Manual connection", subtitle="Bridge address and keys")
        self.ip_row = Adw.EntryRow(title="Bridge IP address", text=self.settings.hue.bridge_ip)
        self.key_row = Adw.PasswordEntryRow(title="App key", text=self.settings.hue.app_key)
        self.client_key_row = Adw.PasswordEntryRow(title="Client key", text=self.settings.hue.client_key)
        for row in (self.ip_row, self.key_row, self.client_key_row):
            manual.add_row(row)
        bridge_group.add(manual)
        page.add(bridge_group)

        area_group = Adw.PreferencesGroup(
            title="Entertainment areas",
            description="You can set up and edit entertainment areas in the official Philips Hue app",
        )
        self.ent_row = Adw.ComboRow(title="Current area")
        refresh_button = Gtk.Button(
            icon_name="view-refresh-symbolic", tooltip_text="Reload areas", valign=Gtk.Align.CENTER
        )
        refresh_button.add_css_class("flat")
        refresh_button.connect("clicked", lambda _b: self._load_entertainment_configs())
        self.ent_row.add_suffix(refresh_button)
        area_group.add(self.ent_row)
        page.add(area_group)
        self._load_entertainment_configs()
        return page

    def _refresh_bridge_status(self):
        self.app_context.get_bridge_status_async(
            lambda status: self.status_row.set_subtitle(
                "Connected to bridge" if status.connected else "Disconnected from bridge"
            )
        )

    def _load_entertainment_configs(self):
        configs = self.bridge.get_entertainment_configurations() if self.bridge.test_connection() else []
        self._entertainment_configs = configs
        if not configs:
            self.ent_row.set_model(Gtk.StringList.new(["No areas found"]))
            return
        labels = []
        for config in configs:
            lights = len(config.get("channels", []))
            labels.append(f"{config.get('name', 'Area')} ({lights} light{'s' if lights != 1 else ''})")
        self.ent_row.set_model(Gtk.StringList.new(labels))
        ids = [c.get("id") for c in configs]
        current = self.settings.hue.entertainment_config_id
        self.ent_row.set_selected(ids.index(current) if current in ids else 0)

    def _on_start_wizard(self, _button):
        wizard = BridgeWizard(app_context=self.settings, on_finished=self._on_wizard_finished)
        wizard.connect("finished", lambda _w: self.pop_subpage())
        wizard.connect("cancelled", lambda _w: self.pop_subpage())
        self.push_subpage(wizard)

    def _on_wizard_finished(self, bridge_ip, app_key, client_key, entertainment_config_id):
        hue = self.settings.hue
        hue.bridge_ip, hue.app_key, hue.client_key = bridge_ip, app_key, client_key
        hue.entertainment_config_id = entertainment_config_id
        self.settings.save()
        self.app_context.apply_settings()
        self.ip_row.set_text(bridge_ip)
        self.key_row.set_text(app_key)
        self.client_key_row.set_text(client_key)
        self._refresh_bridge_status()
        self._load_entertainment_configs()

    # --- General --------------------------------------------------------------

    def _build_general_page(self) -> Adw.PreferencesPage:
        page = Adw.PreferencesPage(title="General", icon_name="preferences-system-symbolic")
        ui = self.settings.ui

        behaviour = Adw.PreferencesGroup(title="App behavior")
        self.startup_row = Adw.SwitchRow(
            title="Start Glowcast on system startup",
            subtitle="Open Glowcast when you log in",
            active=ui.start_at_startup,
        )
        self.startup_row.connect("notify::active", self._on_startup_toggled)
        behaviour.add(self.startup_row)
        behaviour.add(
            switch_row(
                "Start in the tray",
                "Open without showing the window",
                ui.minimize_at_startup,
                self._save(ui, "minimize_at_startup"),
            )
        )

        modes = [key for key, _label in DEFAULT_MODES]
        sync_row = Adw.ExpanderRow(
            title="Start syncing when Glowcast launches",
            show_enable_switch=True,
            enable_expansion=ui.start_sync_at_launch,
        )
        sync_row.connect(
            "notify::enable-expansion",
            lambda r, _p: self._save(ui, "start_sync_at_launch")(r.get_enable_expansion()),
        )
        sync_row.add_row(
            _combo_row(
                "Syncing mode",
                "",
                [label for _key, label in DEFAULT_MODES],
                modes.index(ui.default_mode) if ui.default_mode in modes else 0,
                lambda i: self._save(ui, "default_mode")(modes[i]),
            )
        )
        behaviour.add(sync_row)

        scenes = list(SCENE_PRESETS.items())
        scene_keys = [key for key, _scene in scenes] + [c["id"] for c in self.settings.scenes.custom]
        scene_names = [scene.name for _key, scene in scenes] + [
            c["name"] for c in self.settings.scenes.custom
        ]
        lights_row = Adw.ExpanderRow(
            title="Turn on lights when Glowcast launches",
            show_enable_switch=True,
            enable_expansion=ui.lights_on_at_launch,
        )
        lights_row.connect(
            "notify::enable-expansion",
            lambda r, _p: self._save(ui, "lights_on_at_launch")(r.get_enable_expansion()),
        )
        lights_row.add_row(
            _combo_row(
                "Scene",
                "",
                scene_names,
                scene_keys.index(ui.launch_scene) if ui.launch_scene in scene_keys else 0,
                lambda i: self._save(ui, "launch_scene")(scene_keys[i]),
            )
        )
        behaviour.add(lights_row)

        for title, subtitle, name in (
            ("Turn off lights when Glowcast is closed", "", "lights_off_on_close"),
            ("Enable desktop notifications", "When light sync starts and stops", "notifications"),
            ("Close button quits Glowcast", "Otherwise it keeps running in the tray", "close_quits"),
            (
                "Hide the window while syncing",
                "Keep Glowcast in the tray while lights sync",
                "minimize_to_tray_on_sync",
            ),
        ):
            behaviour.add(switch_row(title, subtitle, getattr(ui, name), self._save(ui, name)))
        page.add(behaviour)

        background = Adw.PreferencesGroup(
            title="Background lighting",
            description="Keep a minimum light level, whatever the screen shows",
        )
        background.add(
            switch_row(
                "Use background lighting with games",
                "",
                self.settings.games.background_light,
                self._save(self.settings.games, "background_light"),
            )
        )
        background.add(
            switch_row(
                "Use background lighting with videos",
                "",
                self.settings.sync.background_light,
                self._save(self.settings.sync, "background_light"),
            )
        )
        page.add(background)

        reset_group = Adw.PreferencesGroup()
        reset_row = Adw.ActionRow(
            title="Reset all settings to defaults",
            subtitle="The bridge connection and monitor choice are kept",
        )
        reset_button = Gtk.Button(label="Reset", valign=Gtk.Align.CENTER)
        reset_button.add_css_class("destructive-action")
        reset_button.connect("clicked", self._on_reset_clicked)
        reset_row.add_suffix(reset_button)
        reset_group.add(reset_row)
        page.add(reset_group)
        return page

    def _on_startup_toggled(self, row, _pspec):
        enabled = row.get_active()
        if enabled == self.settings.ui.start_at_startup:
            return
        row.set_sensitive(False)

        def worker():
            # The desktop may ask for permission first, so not on the GTK thread
            GLib.idle_add(done, set_autostart(enabled))

        def done(ok):
            row.set_sensitive(True)
            if ok:
                self.settings.ui.start_at_startup = enabled
                self.settings.save()
            else:
                row.set_active(not enabled)
                row.set_subtitle("Not allowed by the desktop")
            return GLib.SOURCE_REMOVE

        threading.Thread(target=worker, daemon=True).start()

    def _on_reset_clicked(self, _button):
        self.settings.reset_preferences()
        self.was_reset = True
        self.close()

    # --- Display --------------------------------------------------------------

    def _build_display_page(self) -> Adw.PreferencesPage:
        page = Adw.PreferencesPage(title="Display", icon_name="video-display-symbolic")
        capture = self.settings.capture

        selection = Adw.PreferencesGroup(
            title="Display selection",
            description="The colors on the chosen displays are synchronised with your lights",
        )
        self._displays_row = Adw.ActionRow(title="Displays", subtitle="Pick one or more monitors")
        choose = Gtk.Button(label="Choose Displays", valign=Gtk.Align.CENTER)
        choose.connect("clicked", self._on_choose_displays)
        self._displays_row.add_suffix(choose)
        selection.add(self._displays_row)

        self._area_row = Adw.ActionRow(title="Capture area")
        self._reset_area = Gtk.Button(label="Reset to full screen", valign=Gtk.Align.CENTER)
        self._reset_area.connect("clicked", self._on_reset_capture_area)
        self._area_row.add_suffix(self._reset_area)
        selection.add(self._area_row)
        self._show_capture_area()
        page.add(selection)

        advanced = Adw.PreferencesGroup(title="Advanced")
        delay = Adw.SpinRow(
            title="Delay (ms)",
            subtitle="Hold the light effect back for wireless displays, such as Miracast or Chromecast",
            adjustment=Gtk.Adjustment(
                value=capture.sync_delay_ms,
                lower=0,
                upper=MAX_SYNC_DELAY_MS,
                step_increment=10,
                page_increment=100,
            ),
        )
        delay.connect("notify::value", lambda r, _p: self._save(capture, "sync_delay_ms")(int(r.get_value())))
        advanced.add(delay)
        page.add(advanced)
        return page

    def _show_capture_area(self):
        full = self.settings.capture.capture_area == [0.0, 0.0, 1.0, 1.0]
        self._reset_area.set_sensitive(not full)
        self._area_row.set_subtitle(
            "Full screen. Drag across the Video or Games preview to sync only part of it"
            if full
            else "Part of the screen, as marked on the Video or Games preview"
        )

    def _on_reset_capture_area(self, _button):
        self.settings.capture.capture_area = [0.0, 0.0, 1.0, 1.0]
        self.settings.save()
        self._show_capture_area()

    def _on_choose_displays(self, button):
        capture = self.app_context.capture
        if self.app_context.sync_controller.is_running():
            capture.request_new_selection()  # the running capture reopens the dialog
            return
        button.set_sensitive(False)

        def done(token: str) -> bool:
            button.set_sensitive(True)
            if token:
                capture.set_restore_token(token)
            self._displays_row.set_subtitle("Monitors saved" if token else "No monitors chosen")
            return GLib.SOURCE_REMOVE

        threading.Thread(
            target=lambda: GLib.idle_add(done, ScreenCapture.pick_displays()), daemon=True
        ).start()

    # --- Audio ----------------------------------------------------------------

    def _build_audio_page(self) -> Adw.PreferencesPage:
        page = Adw.PreferencesPage(title="Audio", icon_name="audio-speakers-symbolic")
        music = self.settings.music

        device_group = Adw.PreferencesGroup(
            title="Audio device selection",
            description="The sound on this device is analysed and synchronised with your lights",
        )
        self._device_row = Adw.ComboRow(title="Audio device", model=Gtk.StringList.new(["Loading…"]))
        self._sources: list[tuple[str, str]] = []
        self._device_row.connect("notify::selected", self._on_device_selected)
        device_group.add(self._device_row)
        page.add(device_group)
        threading.Thread(
            target=lambda: GLib.idle_add(self._show_sources, list_sources()), daemon=True
        ).start()

        music_group = Adw.PreferencesGroup(title="Music")
        music_group.add(
            _combo_row(
                "Light channels",
                "How many different light effects the music creates",
                LIGHT_CHANNELS,
                music.color_groups,
                self._save(music, "color_groups"),
            )
        )
        music_group.add(
            switch_row(
                "Vocal filter",
                "Ignore singing, so the lights follow the instruments",
                music.vocal_filter,
                self._save(music, "vocal_filter"),
            )
        )
        page.add(music_group)
        return page

    def _show_sources(self, sources) -> bool:
        self._sources = sources
        names = [name for name, _label in sources]
        current = self.settings.music.audio_device
        if current not in names:
            sources.append((current, current))
            names.append(current)
        self._device_row.set_model(Gtk.StringList.new([label for _name, label in sources]))
        self._device_row.set_selected(names.index(current))
        return GLib.SOURCE_REMOVE

    def _on_device_selected(self, row, _pspec):
        index = row.get_selected()
        if index < len(self._sources):
            self.settings.music.audio_device = self._sources[index][0]
            self.settings.save()

    # --- Keyboard shortcuts ---------------------------------------------------

    def _build_shortcuts_page(self) -> Adw.PreferencesPage:
        page = Adw.PreferencesPage(
            title="Shortcuts", icon_name="preferences-desktop-keyboard-shortcuts-symbolic"
        )
        self._trigger_labels = {}
        for title, ids in (
            ("Controlling Sync Modes", SHORTCUTS[:4]),
            ("Fine-tune your experience", SHORTCUTS[4:]),
        ):
            group = Adw.PreferencesGroup(title=title)
            for shortcut_id, description in ids:
                row = Adw.ActionRow(title=description)
                label = Gtk.Label()
                label.add_css_class("dim-label")
                row.add_suffix(label)
                self._trigger_labels[shortcut_id] = label
                group.add(row)
            page.add(group)

        setup_group = Adw.PreferencesGroup()
        self._shortcut_status = Adw.ActionRow(title="Keyboard shortcuts")
        self._shortcut_button = Gtk.Button(label="Set Up Shortcuts", valign=Gtk.Align.CENTER)
        self._shortcut_button.connect("clicked", self._on_setup_shortcuts)
        self._shortcut_status.add_suffix(self._shortcut_button)
        setup_group.add(self._shortcut_status)
        page.add(setup_group)
        self._show_shortcuts()
        return page

    def _show_shortcuts(self):
        shortcuts = self._shortcuts
        supported = shortcuts is not None and shortcuts.version() > 0
        triggers = shortcuts.triggers if shortcuts else {}
        for shortcut_id, label in self._trigger_labels.items():
            label.set_text(triggers.get(shortcut_id) or "Not set")
        self._shortcut_button.set_sensitive(supported)
        if not supported:
            self._shortcut_status.set_subtitle("Your desktop does not offer global shortcuts to apps")
        elif triggers:
            self._shortcut_status.set_subtitle("Work everywhere, also while Glowcast is in the background")
            self._shortcut_button.set_label("Change Shortcuts")
        else:
            self._shortcut_status.set_subtitle("Your desktop asks which keys to use")

    def _on_setup_shortcuts(self, button):
        if self._shortcuts.triggers and self._shortcuts.version() >= 2:
            self._shortcuts.configure()
            return
        button.set_sensitive(False)

        def work():
            ok = self._shortcuts.bind()
            GLib.idle_add(done, ok)

        def done(ok):
            if ok:
                self.settings.ui.shortcuts = True
                self.settings.save()
            self._show_shortcuts()
            return GLib.SOURCE_REMOVE

        threading.Thread(target=work, daemon=True).start()

    # --- closing --------------------------------------------------------------

    def _on_closed(self, _dialog):
        if self.was_reset:
            return  # keep the defaults rather than this dialog's old values
        hue = self.settings.hue
        hue.bridge_ip = self.ip_row.get_text().strip()
        hue.app_key = self.key_row.get_text().strip()
        hue.client_key = self.client_key_row.get_text().strip()
        selected = self.ent_row.get_selected()
        if selected < len(self._entertainment_configs):
            hue.entertainment_config_id = self._entertainment_configs[selected].get("id", "")
        self.settings.save()
