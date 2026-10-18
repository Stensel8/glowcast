"""Tray icon (StatusNotifierItem), like Hue Sync's notification-area icon.

Served over D-Bus from Glowcast itself with Gio. GNOME shows it with the
AppIndicator extension, KDE natively; without a tray the window simply
stays a normal window.
"""

import os

import gi

gi.require_version("GdkPixbuf", "2.0")
from gi.repository import GdkPixbuf, Gio, GLib

APP_ID = "io.github.stensel8.glowcast"
# Flatpak lets an app own names under its sandbox's id (dev/run.sh may borrow another app's)
BUS_NAME = f"{os.environ.get('FLATPAK_ID', APP_ID)}.Tray"
ITEM_PATH = "/StatusNotifierItem"
MENU_PATH = "/MenuBar"
WATCHERS = ("org.kde.StatusNotifierWatcher", "org.freedesktop.StatusNotifierWatcher")
ICON_FILES = (
    f"/app/share/icons/hicolor/scalable/apps/{APP_ID}.svg",
    os.path.join(os.path.dirname(__file__), "..", "..", "..", "flatpak", f"{APP_ID}.svg"),
)

INTERFACES = Gio.DBusNodeInfo.new_for_xml(
    """
<node>
  <interface name="org.kde.StatusNotifierItem">
    <property name="Category" type="s" access="read"/>
    <property name="Id" type="s" access="read"/>
    <property name="Title" type="s" access="read"/>
    <property name="Status" type="s" access="read"/>
    <property name="IconName" type="s" access="read"/>
    <property name="IconPixmap" type="a(iiay)" access="read"/>
    <property name="ToolTip" type="(sa(iiay)ss)" access="read"/>
    <property name="ItemIsMenu" type="b" access="read"/>
    <property name="Menu" type="o" access="read"/>
    <method name="Activate"><arg type="i" direction="in"/><arg type="i" direction="in"/></method>
    <method name="SecondaryActivate"><arg type="i" direction="in"/><arg type="i" direction="in"/></method>
    <method name="ContextMenu"><arg type="i" direction="in"/><arg type="i" direction="in"/></method>
    <method name="Scroll"><arg type="i" direction="in"/><arg type="s" direction="in"/></method>
    <signal name="NewIcon"/>
    <signal name="NewToolTip"/>
  </interface>
  <interface name="com.canonical.dbusmenu">
    <property name="Version" type="u" access="read"/>
    <property name="TextDirection" type="s" access="read"/>
    <property name="Status" type="s" access="read"/>
    <property name="IconThemePath" type="as" access="read"/>
    <method name="GetLayout">
      <arg type="i" direction="in"/><arg type="i" direction="in"/><arg type="as" direction="in"/>
      <arg type="u" direction="out"/><arg type="(ia{sv}av)" direction="out"/>
    </method>
    <method name="GetGroupProperties">
      <arg type="ai" direction="in"/><arg type="as" direction="in"/>
      <arg type="a(ia{sv})" direction="out"/>
    </method>
    <method name="GetProperty">
      <arg type="i" direction="in"/><arg type="s" direction="in"/><arg type="v" direction="out"/>
    </method>
    <method name="Event">
      <arg type="i" direction="in"/><arg type="s" direction="in"/>
      <arg type="v" direction="in"/><arg type="u" direction="in"/>
    </method>
    <method name="EventGroup">
      <arg type="a(isvu)" direction="in"/><arg type="ai" direction="out"/>
    </method>
    <method name="AboutToShow"><arg type="i" direction="in"/><arg type="b" direction="out"/></method>
    <method name="AboutToShowGroup">
      <arg type="ai" direction="in"/><arg type="ai" direction="out"/><arg type="ai" direction="out"/>
    </method>
    <signal name="LayoutUpdated"><arg type="u"/><arg type="i"/></signal>
  </interface>
</node>"""
)


def _icon_pixmap() -> list:
    """The icon as ARGB pixels, for trays that can't look up the icon name."""
    for path in ICON_FILES:
        try:
            pixbuf = GdkPixbuf.Pixbuf.new_from_file_at_scale(path, 22, 22, True)
        except GLib.Error:
            continue
        if not pixbuf.get_has_alpha():
            pixbuf = pixbuf.add_alpha(False, 0, 0, 0)
        width, height, stride = pixbuf.get_width(), pixbuf.get_height(), pixbuf.get_rowstride()
        rgba = pixbuf.read_pixel_bytes().get_data()
        argb = bytearray()
        for y in range(height):
            row = rgba[y * stride : y * stride + width * 4]
            for x in range(0, width * 4, 4):
                r, g, b, a = row[x : x + 4]
                argb += bytes((a, r, g, b))
        return [(width, height, bytes(argb))]
    return []


class TrayIcon:
    def __init__(self, app, main_window):
        self.app = app
        self.main_window = main_window
        self._syncing = False
        self._revision = 1
        self._pixmap = _icon_pixmap()
        self._bus = app.get_dbus_connection()
        self._registrations = [
            self._bus.register_object(
                ITEM_PATH, INTERFACES.interfaces[0], self._on_item_call, self._item_property
            ),
            self._bus.register_object(
                MENU_PATH, INTERFACES.interfaces[1], self._on_menu_call, self._menu_property
            ),
        ]
        self._name_id = Gio.bus_own_name_on_connection(
            self._bus, BUS_NAME, Gio.BusNameOwnerFlags.NONE, None, None
        )
        self.is_available = self._register()
        # Register again when a tray appears later (e.g. the extension is enabled)
        self._watch_ids = [
            Gio.bus_watch_name_on_connection(
                self._bus, watcher, Gio.BusNameWatcherFlags.NONE, lambda *_: self._register(), None
            )
            for watcher in WATCHERS
        ]

    def _register(self) -> bool:
        for watcher in WATCHERS:
            try:
                self._bus.call_sync(
                    watcher,
                    "/StatusNotifierWatcher",
                    watcher,
                    "RegisterStatusNotifierItem",
                    GLib.Variant("(s)", (BUS_NAME,)),
                    None,
                    Gio.DBusCallFlags.NONE,
                    1000,
                    None,
                )
                return True
            except GLib.Error:
                continue
        return False

    # --- org.kde.StatusNotifierItem -------------------------------------------

    def _item_property(self, _conn, _sender, _path, _iface, name):
        tooltip = "Syncing your lights" if self._syncing else "Not syncing"
        return {
            "Category": GLib.Variant("s", "ApplicationStatus"),
            "Id": GLib.Variant("s", APP_ID),
            "Title": GLib.Variant("s", "Glowcast"),
            "Status": GLib.Variant("s", "Active"),
            "IconName": GLib.Variant("s", APP_ID),
            "IconPixmap": GLib.Variant("a(iiay)", self._pixmap),
            "ToolTip": GLib.Variant("(sa(iiay)ss)", (APP_ID, self._pixmap, "Glowcast", tooltip)),
            "ItemIsMenu": GLib.Variant("b", False),
            "Menu": GLib.Variant("o", MENU_PATH),
        }.get(name)

    def _on_item_call(self, _conn, _sender, _path, _iface, method, _params, invocation):
        if method == "Activate":
            self._action("show")
        elif method == "SecondaryActivate":
            self._action("toggle-sync")
        invocation.return_value(None)

    # --- com.canonical.dbusmenu -------------------------------------------------

    def _items(self) -> list[tuple[int, dict]]:
        label = "Stop light sync" if self._syncing else "Start light sync"
        separator = {"type": GLib.Variant("s", "separator")}
        return [
            (1, {"label": GLib.Variant("s", "Show Glowcast")}),
            (2, separator),
            (3, {"label": GLib.Variant("s", label)}),
            (4, {"label": GLib.Variant("s", "Settings")}),
            (5, separator),
            (6, {"label": GLib.Variant("s", "Quit")}),
        ]

    def _menu_property(self, _conn, _sender, _path, _iface, name):
        return {
            "Version": GLib.Variant("u", 3),
            "TextDirection": GLib.Variant("s", "ltr"),
            "Status": GLib.Variant("s", "normal"),
            "IconThemePath": GLib.Variant("as", []),
        }.get(name)

    def _on_menu_call(self, _conn, _sender, _path, _iface, method, params, invocation):
        items = self._items()
        if method == "GetLayout":
            children = [GLib.Variant("(ia{sv}av)", (i, props, [])) for i, props in items]
            root = (0, {"children-display": GLib.Variant("s", "submenu")}, children)
            invocation.return_value(GLib.Variant("(u(ia{sv}av))", (self._revision, root)))
        elif method == "GetGroupProperties":
            ids = params.unpack()[0]
            wanted = [(i, props) for i, props in items if not ids or i in ids]
            invocation.return_value(GLib.Variant("(a(ia{sv}))", (wanted,)))
        elif method == "GetProperty":
            item_id, name = params.unpack()
            value = dict(items).get(item_id, {}).get(name, GLib.Variant("s", ""))
            invocation.return_value(GLib.Variant("(v)", (value,)))
        elif method == "Event":
            item_id, event, _data, _time = params.unpack()
            if event == "clicked":
                self._clicked(item_id)
            invocation.return_value(None)
        elif method == "EventGroup":
            for item_id, event, _data, _time in params.unpack()[0]:
                if event == "clicked":
                    self._clicked(item_id)
            invocation.return_value(GLib.Variant("(ai)", ([],)))
        elif method == "AboutToShow":
            invocation.return_value(GLib.Variant("(b)", (False,)))
        else:  # AboutToShowGroup
            invocation.return_value(GLib.Variant("(aiai)", ([], [])))

    def _clicked(self, item_id: int) -> None:
        if item_id == 4:
            self.main_window.present()
            self.main_window._on_settings_clicked()
        else:
            self._action({1: "show", 3: "toggle-sync", 6: "quit"}.get(item_id, ""))

    def _action(self, name: str) -> None:
        if name:
            self.app.activate_action(name, None)

    # --- state ------------------------------------------------------------------

    def update_sync_status(self, syncing: bool) -> None:
        if syncing == self._syncing:
            return
        self._syncing = syncing
        self._revision += 1
        layout_updated = GLib.Variant("(ui)", (self._revision, 0))
        self._bus.emit_signal(None, MENU_PATH, "com.canonical.dbusmenu", "LayoutUpdated", layout_updated)
        self._bus.emit_signal(None, ITEM_PATH, "org.kde.StatusNotifierItem", "NewToolTip", None)

    def destroy(self) -> None:
        for watch_id in self._watch_ids:
            Gio.bus_unwatch_name(watch_id)
        Gio.bus_unown_name(self._name_id)
        for registration in self._registrations:
            self._bus.unregister_object(registration)
