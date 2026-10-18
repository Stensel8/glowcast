"""Start Glowcast at login.

Inside Flatpak this goes through the Background portal (no extra sandbox
permissions; the desktop asks the user once). Outside Flatpak an autostart
.desktop file is written directly.
"""

import os
import shlex
import sys
from pathlib import Path

from gi.repository import Gio, GLib

from glowcast import portal

APP_ID = "io.github.stensel8.glowcast"


def in_flatpak() -> bool:
    return os.path.exists("/.flatpak-info")


def set_autostart(enabled: bool) -> bool:
    """Enable or disable starting at login; True if it took effect.

    May block while the desktop asks for permission, so call it from a
    worker thread.
    """
    if in_flatpak():
        bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        results = portal.request(
            bus,
            "org.freedesktop.portal.Background",
            "RequestBackground",
            [("s", "")],
            {
                "reason": GLib.Variant("s", "Start Glowcast when you log in"),
                "autostart": GLib.Variant("b", enabled),
                "commandline": GLib.Variant("as", ["glowcast"]),
            },
            timeout_s=120,
        )
        return results is not None and bool(results.get("autostart")) == enabled

    config_home = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    desktop_file = config_home / "autostart" / f"{APP_ID}.desktop"
    if not enabled:
        desktop_file.unlink(missing_ok=True)
        return True
    desktop_file.parent.mkdir(parents=True, exist_ok=True)
    desktop_file.write_text(
        "[Desktop Entry]\n"
        "Type=Application\n"
        "Name=Glowcast\n"
        f"Exec={shlex.join([sys.executable, '-m', 'glowcast'])}\n"
        f"Icon={APP_ID}\n"
        "X-GNOME-Autostart-enabled=true\n"
    )
    return True
