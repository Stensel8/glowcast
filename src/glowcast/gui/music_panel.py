"""Music panel: colour palettes (built in and your own), intensity, brightness, stereo, meters."""

import math
import uuid

from gi.repository import Adw, Gdk, GLib, Gtk

from glowcast.gui.widgets import (
    ColorSetEditor,
    brightness_row,
    intensity_row,
    mark_active,
    new_tile,
    switch_row,
    tile,
)
from glowcast.music import palette_colors
from glowcast.presets import DEFAULT_PALETTE, MUSIC_PALETTES

METER_RISE_S = 0.04  # meter time constants: quick rise, slower fall (VU style)
METER_FALL_S = 0.15


class MusicPanel(Gtk.Box):
    def __init__(self, app_context, brightness: Gtk.Adjustment):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=18)
        self._music_controller = app_context.music_controller
        self._settings = app_context.settings
        self._music = self._settings.music
        self._tick_id = None
        self._last_frame_time = None

        self._palettes = Gtk.FlowBox(
            selection_mode=Gtk.SelectionMode.NONE, max_children_per_line=8, homogeneous=True
        )
        self._palettes.add_css_class("card")
        self._palettes.add_css_class("palette-card")
        self._fill_palettes()
        self.append(self._palettes)

        group = Adw.PreferencesGroup()
        group.add(
            intensity_row(
                self._music.intensity, self._on_intensity_changed, "How strongly the lights react to the beat"
            )
        )
        group.add(brightness_row(brightness))
        group.add(
            switch_row(
                "Stereo",
                "Left and right audio drive left and right lights",
                self._music.localization,
                self._on_stereo_changed,
            )
        )
        self.append(group)
        self.append(self._build_meters())

    # --- palettes --------------------------------------------------------------

    def _fill_palettes(self) -> None:
        self._palettes.remove_all()
        self._palette_buttons = {}
        entries = [(key, p.name, p.colors) for key, p in MUSIC_PALETTES.items()]
        entries += [(p["id"], p["name"], p["colors"]) for p in self._music.custom_palettes]
        for key, name, colors in entries:
            custom = key not in MUSIC_PALETTES
            button = tile(colors, name, tooltip=f"{name} — click again to edit" if custom else name)
            button.connect("clicked", self._on_palette_clicked, key)
            self._palettes.append(button)
            self._palette_buttons[key] = button
        add = new_tile("New palette")
        add.connect("clicked", lambda _b: self._edit_palette(None))
        self._palettes.append(add)
        mark_active(self._palette_buttons, self._music.palette)
        if hasattr(self, "_meter_css"):
            self._color_meters()

    def _on_palette_clicked(self, _button, key: str) -> None:
        if key == self._music.palette and key not in MUSIC_PALETTES:
            self._edit_palette(key)
            return
        self._music.palette = key  # the music loop reads this every frame
        mark_active(self._palette_buttons, key)
        self._color_meters()
        self._settings.save()

    def _edit_palette(self, key: str | None) -> None:
        palettes = self._music.custom_palettes
        entry = next((p for p in palettes if p["id"] == key), None)

        def save(name: str, colors: list[str]) -> None:
            if entry:
                entry.update(name=name, colors=colors)
            else:
                palettes.append({"id": f"custom-{uuid.uuid4().hex[:8]}", "name": name, "colors": colors})
                self._music.palette = palettes[-1]["id"]
            self._settings.save()
            self._fill_palettes()

        def delete() -> None:
            palettes.remove(entry)
            self._music.palette = DEFAULT_PALETTE
            self._settings.save()
            self._fill_palettes()

        ColorSetEditor(
            "Edit Palette" if entry else "New Palette",
            entry["name"] if entry else f"My palette {len(palettes) + 1}",
            entry["colors"] if entry else list(palette_colors(self._music)),
            save,
            delete if entry else None,
            "Are you sure that you want to permanently delete this palette?",
        ).present(self)

    # --- meters ----------------------------------------------------------------

    def _build_meters(self) -> Gtk.Widget:
        card = Gtk.Grid(column_spacing=12, row_spacing=8)
        card.add_css_class("card")
        card.add_css_class("meter-card")
        self._meters = {}
        for row, name in enumerate(("Bass", "Mid", "High")):
            bar = Gtk.LevelBar(min_value=0.0, max_value=1.0, hexpand=True, valign=Gtk.Align.CENTER)
            for offset in ("low", "high", "full"):
                bar.remove_offset_value(offset)
            bar.add_css_class(f"meter-{name.lower()}")
            card.attach(Gtk.Label(label=name, xalign=0), 0, row, 1, 1)
            card.attach(bar, 1, row, 1, 1)
            self._meters[name] = bar
        self._meter_css = Gtk.CssProvider()
        Gtk.StyleContext.add_provider_for_display(
            Gdk.Display.get_default(), self._meter_css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
        )
        self._color_meters()
        return card

    def _color_meters(self) -> None:
        """Bass, mid and high in colours of the current palette."""
        colors = palette_colors(self._music)
        picks = (colors[0], colors[len(colors) // 2], colors[-1])
        self._meter_css.load_from_string(
            "".join(
                f".meter-{name} block.filled {{ background-color: {color}; }}"
                for name, color in zip(("bass", "mid", "high"), picks)
            )
        )

    def set_running(self, running: bool) -> None:
        """Animate the level meters only while music sync runs."""
        if running and self._tick_id is None:
            self._last_frame_time = None
            self._tick_id = self.add_tick_callback(self._on_tick)
        elif not running and self._tick_id is not None:
            self.remove_tick_callback(self._tick_id)
            self._tick_id = None
            for bar in self._meters.values():
                bar.set_value(0.0)

    def _on_tick(self, _widget, frame_clock) -> bool:
        """Ease the meters towards the newest audio levels every display frame."""
        now = frame_clock.get_frame_time() / 1_000_000
        dt = 0.0 if self._last_frame_time is None else now - self._last_frame_time
        self._last_frame_time = now

        features = self._music_controller.latest_features
        targets = (features.bass, features.mid, features.high) if features else (0, 0, 0)
        for name, target in zip(("Bass", "Mid", "High"), targets):
            bar = self._meters[name]
            value = bar.get_value()
            time_constant = METER_RISE_S if target > value else METER_FALL_S
            bar.set_value(value + (target - value) * (1 - math.exp(-dt / time_constant)))
        return GLib.SOURCE_CONTINUE

    # The music loop reads these settings every frame, so changes apply live.

    def _on_intensity_changed(self, intensity: str) -> None:
        self._music.intensity = intensity
        self._settings.save()

    def _on_stereo_changed(self, active: bool) -> None:
        self._music.localization = active
        self._settings.save()
