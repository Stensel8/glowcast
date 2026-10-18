"""Video and games panel: live preview, intensity, brightness, audio, capture area, displays."""

import threading
from collections.abc import Callable

from gi.repository import Adw, GLib, Gtk

from glowcast.capture import ScreenCapture
from glowcast.gui.widgets import brightness_row, intensity_row, switch_row
from glowcast.gui.zone_preview_widget import FULL_AREA, ZonePreviewWidget

INTENSITY_HINT = {
    "video": "How quickly the lights follow the screen",
    "games": "How fast and punchy the lights react to the game",
}


class VideoPanel(Gtk.Box):
    def __init__(
        self, app_context, mode: str, on_capture_area_changed: Callable[[], None], brightness: Gtk.Adjustment
    ):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=18)
        self._app_context = app_context
        self._settings = app_context.settings
        self._video = self._settings.video_settings(mode)
        self._on_capture_area_changed = on_capture_area_changed

        zones = app_context.sync_controller.zone_processor
        self.preview = ZonePreviewWidget(zones.rows, zones.cols, self._on_area_dragged)
        self.preview.set_size_request(-1, 240)
        card = Gtk.Box()
        card.add_css_class("card")
        card.add_css_class("preview-card")
        card.append(self.preview)
        self.append(card)

        group = Adw.PreferencesGroup()
        group.add(intensity_row(self._video.intensity, self._on_intensity_changed, INTENSITY_HINT[mode]))
        group.add(brightness_row(brightness))
        group.add(
            switch_row(
                "Use audio for effect",
                "The lights also react to what you hear",
                self._video.use_audio,
                self._on_use_audio_changed,
            )
        )

        self._area_row = Adw.ActionRow(title="Capture area")
        self._reset_area = Gtk.Button(label="Reset to full screen", valign=Gtk.Align.CENTER)
        self._reset_area.connect("clicked", lambda _b: self._set_capture_area(FULL_AREA))
        self._area_row.add_suffix(self._reset_area)
        group.add(self._area_row)

        self._displays_row = Adw.ActionRow(
            title="Displays", subtitle="Pick one or more monitors in the screen-share dialog"
        )
        self._choose_button = Gtk.Button(label="Choose Displays", valign=Gtk.Align.CENTER)
        self._choose_button.connect("clicked", self._on_choose_displays)
        self._displays_row.add_suffix(self._choose_button)
        group.add(self._displays_row)
        self.append(group)
        self.show_capture_area()

    def update_preview(self, data: dict) -> None:
        self.preview.update(data["zones"], data.get("frame"))

    def clear_preview(self) -> None:
        self.preview.clear()

    def show_capture_area(self) -> None:
        area = tuple(self._settings.capture.capture_area)
        full = area == FULL_AREA
        self.preview.set_capture_area(area)
        self._reset_area.set_sensitive(not full)
        self._area_row.set_subtitle(
            "Drag across the preview to sync only part of the screen"
            if full
            else "Syncing the part of the screen you marked"
        )

    def _on_area_dragged(self, area: tuple) -> None:
        self._set_capture_area(area)

    def _set_capture_area(self, area: tuple) -> None:
        self._settings.capture.capture_area = [round(v, 4) for v in area]
        self._settings.save()
        self._on_capture_area_changed()

    def _on_intensity_changed(self, intensity: str) -> None:
        self._video.intensity = intensity
        self._settings.save()

    def _on_use_audio_changed(self, active: bool) -> None:
        self._video.use_audio = active
        self._settings.save()

    def _on_choose_displays(self, _button) -> None:
        capture = self._app_context.capture
        self._displays_row.set_subtitle("Pick one or more monitors in the dialog")
        if self._app_context.sync_controller.is_running():
            capture.request_new_selection()  # the running capture reopens the dialog
            return

        self._choose_button.set_sensitive(False)

        def done(token: str) -> bool:
            self._choose_button.set_sensitive(True)
            if token:
                capture.set_restore_token(token)
            self._displays_row.set_subtitle(
                "Monitors saved for video and game sync" if token else "No monitors chosen"
            )
            return GLib.SOURCE_REMOVE

        threading.Thread(
            target=lambda: GLib.idle_add(done, ScreenCapture.pick_displays()), daemon=True
        ).start()
