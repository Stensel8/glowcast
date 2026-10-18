"""Keyboard shortcuts that work while Glowcast is in the background.

Hue Sync registers system-wide hotkeys itself; on Wayland only the desktop
may, so shortcuts go through the GlobalShortcuts portal: the desktop asks the
user which keys to use and tells Glowcast when they are pressed.
"""

from collections.abc import Callable

from gi.repository import Gio, GLib

from glowcast import portal

INTERFACE = "org.freedesktop.portal.GlobalShortcuts"

# Hue Sync's shortcuts; the ids double as application action names.
SHORTCUTS = (
    ("toggle-sync", "Toggle Light Sync (last used)"),
    ("toggle-video", "Toggle Video Sync"),
    ("toggle-music", "Toggle Music Sync"),
    ("toggle-games", "Toggle Game Sync"),
    ("brightness-up", "Increase brightness"),
    ("brightness-down", "Decrease brightness"),
    ("toggle-lights", "Turn lights on / off"),
)


class GlobalShortcuts:
    def __init__(self, on_activated: Callable[[str], None]):
        self._bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        self._session: str | None = None
        self._on_activated = on_activated
        self.triggers: dict[str, str] = {}  # shortcut id -> keys, as the desktop describes them
        # Subscribed on the GTK thread, so presses arrive there.
        self._bus.signal_subscribe(
            portal.BUS_NAME,
            INTERFACE,
            "Activated",
            portal.OBJECT_PATH,
            None,
            Gio.DBusSignalFlags.NONE,
            self._on_signal,
        )

    def version(self) -> int:
        """Portal version; 0 when the desktop has no global shortcuts."""
        try:
            reply = self._bus.call_sync(
                portal.BUS_NAME,
                portal.OBJECT_PATH,
                "org.freedesktop.DBus.Properties",
                "Get",
                GLib.Variant("(ss)", (INTERFACE, "version")),
                GLib.VariantType("(v)"),
                Gio.DBusCallFlags.NONE,
                2000,
                None,
            )
            return reply.unpack()[0]
        except GLib.Error:
            return 0

    def bind(self) -> bool:
        """Register the shortcuts; the desktop asks for keys it doesn't know yet.

        Blocks while the desktop shows its dialog: call from a worker thread.
        """
        if self._session is None:
            results = portal.request(
                self._bus,
                INTERFACE,
                "CreateSession",
                [],
                {"session_handle_token": GLib.Variant("s", "glowcast_shortcuts")},
            )
            if not results:
                return False
            self._session = results["session_handle"]
        shortcuts = [(sid, {"description": GLib.Variant("s", text)}) for sid, text in SHORTCUTS]
        results = portal.request(
            self._bus,
            INTERFACE,
            "BindShortcuts",
            [("o", self._session), ("a(sa{sv})", shortcuts), ("s", "")],
            {},
            timeout_s=300,
        )
        if results is None:
            return False
        self.triggers = {
            sid: info.get("trigger_description", "") for sid, info in results.get("shortcuts", [])
        }
        return True

    def configure(self) -> None:
        """Open the desktop's settings for these shortcuts (portal version 2)."""
        if self._session and self.version() >= 2:
            self._bus.call(
                portal.BUS_NAME,
                portal.OBJECT_PATH,
                INTERFACE,
                "ConfigureShortcuts",
                GLib.Variant("(osa{sv})", (self._session, "", {})),
                None,
                Gio.DBusCallFlags.NONE,
                -1,
                None,
                None,
            )

    def _on_signal(self, _conn, _sender, _path, _iface, _signal, params, *_):
        session, shortcut_id, _timestamp, _options = params.unpack()
        if session == self._session:
            self._on_activated(shortcut_id)
