"""Scenes panel: Hue Sync's scene gallery, your own scenes and your bridge's scenes."""

import threading
import uuid
from collections.abc import Callable

from gi.repository import Adw, GLib, Gtk

from glowcast.gui.widgets import (
    ColorSetEditor,
    brightness_row,
    mark_active,
    new_scene_card,
    scene_card,
    switch_row,
)
from glowcast.presets import DEFAULT_SCENE, SCENE_PRESETS


def _flowbox() -> Gtk.FlowBox:
    box = Gtk.FlowBox(
        selection_mode=Gtk.SelectionMode.NONE, max_children_per_line=7, homogeneous=True, row_spacing=6
    )
    box.add_css_class("card")
    box.add_css_class("palette-card")
    return box


class ScenesPanel(Gtk.Box):
    """Calls on_changed() whenever the chosen scene or Dynamics change."""

    def __init__(self, app_context, on_changed: Callable[[], None], brightness: Gtk.Adjustment):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=18)
        self._app_context = app_context
        self._settings = app_context.settings
        self._scenes = self._settings.scenes
        self._on_changed = on_changed
        self._buttons: dict[str, Gtk.Button] = {}

        self._gallery = _flowbox()
        self.append(self._gallery)

        self._bridge_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        heading = Gtk.Label(label="From your bridge", xalign=0)
        heading.add_css_class("heading")
        self._bridge_status = Gtk.Label(xalign=0, wrap=True)
        self._bridge_status.add_css_class("dim-label")
        # One grid per room or zone, as in the Hue app: the same scene name
        # (e.g. "Read") often exists in several rooms.
        self._bridge_groups = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        for widget in (heading, self._bridge_status, self._bridge_groups):
            self._bridge_box.append(widget)
        self.append(self._bridge_box)
        self._fill_gallery()

        group = Adw.PreferencesGroup()
        group.add(brightness_row(brightness))
        group.add(
            switch_row(
                "Dynamics",
                "Colours slowly move between the lights",
                self._scenes.dynamic,
                self._on_dynamic_changed,
            )
        )
        self.append(group)
        self.load_bridge_scenes()

    # --- tiles -----------------------------------------------------------------

    def _fill_gallery(self) -> None:
        self._gallery.remove_all()
        for key in [k for k in self._buttons if not k.startswith("bridge:")]:
            del self._buttons[key]
        for key, scene in SCENE_PRESETS.items():
            self._add_tile(self._gallery, key, scene.name, scene.colors)
        for entry in self._scenes.custom:
            self._add_tile(self._gallery, entry["id"], entry["name"], entry["colors"], custom=True)
        add = new_scene_card("Create scene")
        add.connect("clicked", lambda _b: self._edit_scene(None))
        self._gallery.append(add)
        mark_active(self._buttons, self._scenes.scene)

    def _add_tile(self, box, key, name, colors, custom=False, tooltip="") -> None:
        button = scene_card(colors, name, f"{name} — click again to edit" if custom else tooltip)
        button.connect("clicked", self._on_tile_clicked, key, custom)
        box.append(button)
        self._buttons[key] = button

    def load_bridge_scenes(self) -> None:
        """Fetch the bridge's scenes for this area (in the background)."""
        self._bridge_status.set_text("Loading scenes…")
        self._bridge_status.set_visible(True)

        def work():
            scenes = self._app_context.scenes.list_bridge_scenes()
            GLib.idle_add(self._show_bridge_scenes, scenes)

        threading.Thread(target=work, daemon=True).start()

    def _show_bridge_scenes(self, scenes) -> bool:
        while child := self._bridge_groups.get_first_child():
            self._bridge_groups.remove(child)
        for key in [k for k in self._buttons if k.startswith("bridge:")]:
            del self._buttons[key]
        groups: dict[str, list] = {}
        for scene in scenes:  # already sorted by room/zone, then name
            groups.setdefault(scene.group or "Other", []).append(scene)
        for group, members in groups.items():
            if len(groups) > 1:
                label = Gtk.Label(label=group, xalign=0, margin_top=4)
                label.add_css_class("caption-heading")
                label.add_css_class("dim-label")
                self._bridge_groups.append(label)
            grid = _flowbox()
            for scene in members:
                self._add_tile(grid, scene.id, scene.name, scene.colors, tooltip=f"{scene.name} ({group})")
            self._bridge_groups.append(grid)
        self._bridge_status.set_text(
            "" if scenes else "No scenes found for the rooms and zones of this entertainment area"
        )
        self._bridge_status.set_visible(not scenes)
        mark_active(self._buttons, self._scenes.scene)
        return GLib.SOURCE_REMOVE

    def _on_tile_clicked(self, _button, key: str, custom: bool) -> None:
        if custom and key == self._scenes.scene:
            self._edit_scene(key)
            return
        self._scenes.scene = key
        mark_active(self._buttons, key)
        self._settings.save()
        self._on_changed()

    def _edit_scene(self, key: str | None) -> None:
        custom = self._scenes.custom
        entry = next((c for c in custom if c["id"] == key), None)
        current = self._app_context.scenes.scene(self._scenes.scene, custom)

        def save(name: str, colors: list[str]) -> None:
            if entry:
                entry.update(name=name, colors=colors)
            else:
                custom.append({"id": f"scene-{uuid.uuid4().hex[:8]}", "name": name, "colors": colors})
                self._scenes.scene = custom[-1]["id"]
            self._settings.save()
            self._fill_gallery()
            self._on_changed()

        def delete() -> None:
            custom.remove(entry)
            self._scenes.scene = DEFAULT_SCENE
            self._settings.save()
            self._fill_gallery()

        ColorSetEditor(
            "Edit Scene" if entry else "Create Scene",
            entry["name"] if entry else f"Scene {len(custom) + 1}",
            entry["colors"] if entry else list(current.colors if current else ()),
            save,
            delete if entry else None,
            "Are you sure that you want to permanently delete this scene?",
        ).present(self)

    # --- dynamics ---------------------------------------------------------------

    def _on_dynamic_changed(self, active: bool) -> None:
        self._scenes.dynamic = active
        self._settings.save()
        self._on_changed()
