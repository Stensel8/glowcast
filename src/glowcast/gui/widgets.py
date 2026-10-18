"""Small widgets shared by the mode panels."""

import math
import random
from collections.abc import Callable

import cairo
from gi.repository import Adw, Gdk, GLib, Gtk, Pango

from glowcast.colors import hex_to_rgb, rgb_to_hex
from glowcast.presets import INTENSITY_LABELS


def intensity_row(active: str, on_changed: Callable[[str], None], subtitle: str) -> Adw.ActionRow:
    """Row with linked Subtle / Moderate / High / Extreme toggle buttons."""
    row = Adw.ActionRow(title="Intensity", subtitle=subtitle)
    buttons = Gtk.Box(valign=Gtk.Align.CENTER)
    buttons.add_css_class("linked")
    group = None
    for key, label in INTENSITY_LABELS.items():
        button = Gtk.ToggleButton(label=label, active=key == active)
        button.set_group(group)
        group = group or button
        button.connect("toggled", lambda b, k=key: b.get_active() and on_changed(k))
        buttons.append(button)
    row.add_suffix(buttons)
    return row


def brightness_row(adjustment: Gtk.Adjustment) -> Adw.ActionRow:
    """The brightness slider. Every mode shares one adjustment, like Hue Sync's single slider."""
    row = Adw.ActionRow(title="Brightness")
    scale = Gtk.Scale(adjustment=adjustment, draw_value=False, hexpand=True, valign=Gtk.Align.CENTER)
    scale.set_size_request(220, -1)
    row.add_suffix(scale)
    return row


def switch_row(title: str, subtitle: str, active: bool, on_changed: Callable[[bool], None]) -> Adw.SwitchRow:
    row = Adw.SwitchRow(title=title, subtitle=subtitle, active=active)
    row.connect("notify::active", lambda r, _p: on_changed(r.get_active()))
    return row


class Swatch(Gtk.DrawingArea):
    """Rounded rectangle filled with a left-to-right gradient of colours."""

    def __init__(self, colors, width: int, height: int, radius: float = 8):
        super().__init__(content_width=width, content_height=height)
        self._radius = radius
        self.set_colors(colors)
        self.set_draw_func(self._draw)

    def set_colors(self, colors) -> None:
        self._colors = [hex_to_rgb(c) for c in colors] or [(0.5, 0.5, 0.5)]
        self.queue_draw()

    def _draw(self, _area, ctx, width, height):
        r = min(self._radius, width / 2, height / 2)
        ctx.new_sub_path()
        ctx.arc(width - r, r, r, -1.5708, 0)
        ctx.arc(width - r, height - r, r, 0, 1.5708)
        ctx.arc(r, height - r, r, 1.5708, 3.1416)
        ctx.arc(r, r, r, 3.1416, 4.7124)
        ctx.close_path()
        gradient = cairo.LinearGradient(0, 0, width, 0)
        for i, rgb in enumerate(self._colors):
            gradient.add_color_stop_rgb(i / max(1, len(self._colors) - 1), *rgb)
        ctx.set_source(gradient)
        ctx.fill()


class SceneArt(Gtk.DrawingArea):
    """A round scene picture painted from the scene's own colours.

    Hue Sync shows a photo per scene; these are Glowcast's own artwork: soft
    glowing blobs, placed the same way every time for a given scene name.
    """

    def __init__(self, colors, name: str, size: int = 72):
        super().__init__(content_width=size, content_height=size, halign=Gtk.Align.CENTER)
        rgbs = [hex_to_rgb(c) for c in colors] or [(0.6, 0.6, 0.6)]
        darken = 0.5
        if len(rgbs) == 1:  # a white scene: soft lighter and darker shades give it depth
            color = rgbs[0]
            rgbs = [color, tuple(c * 0.88 for c in color), tuple(c + (1 - c) * 0.6 for c in color)]
            darken = 0.8
        rng = random.Random(name)
        self._base = tuple(sum(c[i] for c in rgbs) / len(rgbs) * darken for i in range(3))
        self._blobs = [
            (rng.uniform(0.15, 0.85), rng.uniform(0.15, 0.85), rng.uniform(0.35, 0.65), rgbs[i % len(rgbs)])
            for i in range(max(4, len(rgbs)))
        ]
        self.set_draw_func(self._draw)

    def _draw(self, _area, ctx, width, height):
        size = min(width, height)
        ctx.translate((width - size) / 2, (height - size) / 2)
        ctx.arc(size / 2, size / 2, size / 2, 0, 2 * math.pi)
        ctx.clip()
        ctx.set_source_rgb(*self._base)
        ctx.paint()
        for x, y, radius, (r, g, b) in self._blobs:
            glow = cairo.RadialGradient(x * size, y * size, 0, x * size, y * size, radius * size)
            glow.add_color_stop_rgba(0, r, g, b, 0.95)
            glow.add_color_stop_rgba(1, r, g, b, 0)
            ctx.set_source(glow)
            ctx.paint()
        shine = cairo.RadialGradient(size * 0.35, size * 0.3, 0, size * 0.35, size * 0.3, size * 0.6)
        shine.add_color_stop_rgba(0, 1, 1, 1, 0.2)
        shine.add_color_stop_rgba(1, 1, 1, 1, 0)
        ctx.set_source(shine)
        ctx.paint()
        edge = cairo.RadialGradient(size / 2, size / 2, size * 0.3, size / 2, size / 2, size / 2)
        edge.add_color_stop_rgba(0, 0, 0, 0, 0)
        edge.add_color_stop_rgba(1, 0, 0, 0, 0.3)
        ctx.set_source(edge)
        ctx.paint()


def scene_card(colors, name: str, tooltip: str = "") -> Gtk.Button:
    """Hue Sync's scene card shape: a round picture with the name below."""
    content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
    content.append(SceneArt(colors, name))
    content.append(_card_label(name))
    return _card_button(content, tooltip or name)


def new_scene_card(label: str) -> Gtk.Button:
    content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
    plus = Gtk.Image(icon_name="list-add-symbolic", pixel_size=24, halign=Gtk.Align.CENTER)
    plus.set_size_request(72, 72)
    plus.add_css_class("new-scene")
    content.append(plus)
    content.append(_card_label(label))
    return _card_button(content, label)


def _card_label(text: str) -> Gtk.Label:
    label = Gtk.Label(
        label=text,
        wrap=True,
        lines=2,
        ellipsize=Pango.EllipsizeMode.END,
        justify=Gtk.Justification.CENTER,
        max_width_chars=11,
        width_chars=11,
    )
    label.add_css_class("caption")
    return label


def _card_button(content: Gtk.Widget, tooltip: str) -> Gtk.Button:
    button = Gtk.Button(child=content, tooltip_text=tooltip, valign=Gtk.Align.START)
    button.add_css_class("flat")
    button.add_css_class("scene-card")
    return button


def tile(colors, label: str, width: int = 64, height: int = 36, tooltip: str = "") -> Gtk.Button:
    """Swatch with a caption, as a flat button (palettes and scenes)."""
    content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
    content.append(Swatch(colors, width, height))
    caption = Gtk.Label(label=label, ellipsize=Pango.EllipsizeMode.END, max_width_chars=12)
    caption.add_css_class("caption")
    content.append(caption)
    button = Gtk.Button(child=content, tooltip_text=tooltip or label)
    button.add_css_class("flat")
    button.add_css_class("palette-tile")
    return button


def new_tile(label: str, width: int = 64, height: int = 36) -> Gtk.Button:
    """The "+" tile at the end of a palette or scene grid."""
    content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
    plus = Gtk.Image(icon_name="list-add-symbolic", pixel_size=20)
    plus.set_size_request(width, height)
    plus.add_css_class("new-tile")
    content.append(plus)
    caption = Gtk.Label(label=label)
    caption.add_css_class("caption")
    content.append(caption)
    button = Gtk.Button(child=content, tooltip_text=label)
    button.add_css_class("flat")
    button.add_css_class("palette-tile")
    return button


def mark_active(buttons: dict, key: str) -> None:
    for button_key, button in buttons.items():
        if button_key == key:
            button.add_css_class("palette-active")
        else:
            button.remove_css_class("palette-active")


class ColorSetEditor(Adw.Dialog):
    """A name and five colours: your own music palettes and scenes."""

    def __init__(
        self,
        title: str,
        name: str,
        colors: list[str],
        on_save: Callable[[str, list[str]], None],
        on_delete: Callable[[], None] | None = None,
        delete_question: str = "",
    ):
        super().__init__(title=title, content_width=420)
        self._on_save = on_save
        header = Adw.HeaderBar(show_end_title_buttons=False, show_start_title_buttons=False)
        cancel = Gtk.Button(label="Cancel")
        cancel.connect("clicked", lambda _b: self.close())
        header.pack_start(cancel)
        save = Gtk.Button(label="Save")
        save.add_css_class("suggested-action")
        save.connect("clicked", self._on_save_clicked)
        header.pack_end(save)

        page = Adw.PreferencesPage()
        group = Adw.PreferencesGroup()
        self._name = Adw.EntryRow(title="Name", text=name)
        group.add(self._name)
        page.add(group)

        colors_group = Adw.PreferencesGroup(title="Colors")
        row = Gtk.Box(spacing=8, halign=Gtk.Align.CENTER, margin_top=6, margin_bottom=6)
        self._buttons = []
        for color in (list(colors) + ["#FFFFFF"] * 5)[:5]:
            rgba = Gdk.RGBA()
            rgba.parse(color)
            button = Gtk.ColorDialogButton(dialog=Gtk.ColorDialog(with_alpha=False), rgba=rgba)
            self._buttons.append(button)
            row.append(button)
        colors_group.add(row)
        page.add(colors_group)

        if on_delete:
            delete_group = Adw.PreferencesGroup()
            delete = Gtk.Button(label="Delete", halign=Gtk.Align.CENTER)
            delete.add_css_class("destructive-action")
            delete.add_css_class("pill")
            delete.connect("clicked", self._confirm_delete, on_delete, delete_question)
            delete_group.add(delete)
            page.add(delete_group)

        view = Adw.ToolbarView(content=page)
        view.add_top_bar(header)
        self.set_child(view)

    def _on_save_clicked(self, _button) -> None:
        colors = [rgb_to_hex((c.red, c.green, c.blue)) for c in (b.get_rgba() for b in self._buttons)]
        self._on_save(self._name.get_text().strip() or "Untitled", colors)
        self.close()

    def _confirm_delete(self, _button, on_delete, question: str) -> None:
        alert = Adw.AlertDialog(heading="Delete?", body=question)
        alert.add_response("cancel", "Cancel")
        alert.add_response("delete", "Delete")
        alert.set_response_appearance("delete", Adw.ResponseAppearance.DESTRUCTIVE)

        def respond(_dialog, response):
            if response == "delete":
                on_delete()
                self.close()

        alert.connect("response", respond)
        alert.present(self)


class DebouncedSave:
    """Write settings to disk once changes (e.g. a dragged slider) settle."""

    def __init__(self, settings, delay_ms: int = 500):
        self._settings = settings
        self._delay_ms = delay_ms
        self._source_id = None

    def __call__(self) -> None:
        if self._source_id:
            GLib.source_remove(self._source_id)
        self._source_id = GLib.timeout_add(self._delay_ms, self._save)

    def flush(self) -> None:
        if self._source_id:
            GLib.source_remove(self._source_id)
            self._save()

    def _save(self) -> bool:
        self._source_id = None
        self._settings.save()
        return GLib.SOURCE_REMOVE
