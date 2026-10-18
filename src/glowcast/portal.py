"""Calling xdg-desktop-portal methods that answer through a Request object."""

import uuid

from gi.repository import Gio, GLib

from glowcast.utils.logging import timed_print

BUS_NAME = "org.freedesktop.portal.Desktop"
OBJECT_PATH = "/org/freedesktop/portal/desktop"


def request(
    bus: Gio.DBusConnection,
    interface: str,
    method: str,
    args: list,
    options: dict,
    timeout_s: int = 30,
) -> dict | None:
    """Call a portal method and wait for its Request::Response.

    args are (signature, value) pairs preceding the options dict. Returns the
    response results, or None if the user cancelled or the call failed. The
    Response handler is subscribed before the call (on a private main
    context) so a fast reply cannot be missed. Blocks; call it from a worker
    thread when the portal may show a dialog.
    """
    token = f"glowcast_{uuid.uuid4().hex}"
    options = dict(options, handle_token=GLib.Variant("s", token))
    sender = bus.get_unique_name()[1:].replace(".", "_")
    request_path = f"{OBJECT_PATH}/request/{sender}/{token}"

    context = GLib.MainContext.new()
    loop = GLib.MainLoop.new(context, False)
    response = {}

    def on_response(_conn, _sender, _path, _iface, _signal, params, *_):
        response["code"], response["results"] = params.unpack()
        loop.quit()

    context.push_thread_default()
    try:
        subscription = bus.signal_subscribe(
            BUS_NAME,
            "org.freedesktop.portal.Request",
            "Response",
            request_path,
            None,
            Gio.DBusSignalFlags.NONE,
            on_response,
        )
        try:
            signature = "(" + "".join(sig for sig, _ in args) + "a{sv})"
            values = (*(value for _, value in args), options)
            bus.call_sync(
                BUS_NAME,
                OBJECT_PATH,
                interface,
                method,
                GLib.Variant(signature, values),
                GLib.VariantType("(o)"),
                Gio.DBusCallFlags.NONE,
                -1,
                None,
            )
            timeout = GLib.timeout_source_new_seconds(timeout_s)
            timeout.set_callback(lambda *_: loop.quit() or GLib.SOURCE_REMOVE)
            timeout.attach(context)
            loop.run()
            timeout.destroy()
        finally:
            bus.signal_unsubscribe(subscription)
    except GLib.Error as e:
        timed_print(f"Portal {method} failed: {e.message}")
        return None
    finally:
        context.pop_thread_default()

    code = response.get("code")
    if code != 0:
        reason = {None: "timed out", 1: "cancelled by user"}.get(code, f"failed (code {code})")
        timed_print(f"Portal {method} {reason}")
        return None
    return response["results"]
