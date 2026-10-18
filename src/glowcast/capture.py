"""Screen capture using PipeWire portal with optimized GStreamer pipeline.

Every stream the user shares in the portal dialog (usually one per monitor)
gets its own pipeline; capture() returns them combined into one image, side
by side or stacked depending on how the monitors are arranged.
"""

import os
import re
import threading
import time
import uuid

import gi
import numpy as np

gi.require_version("Gst", "1.0")
gi.require_version("GstApp", "1.0")
gi.require_version("GstVideo", "1.0")
import contextlib

from gi.repository import Gio, GLib, Gst, GstApp, GstVideo

from glowcast import portal
from glowcast.black_bar_detector import BlackBarDetector
from glowcast.utils.logging import timed_print

Gst.init(None)

SCREENCAST_IFACE = "org.freedesktop.portal.ScreenCast"
CURSOR_MODE_HIDDEN = 1  # ScreenCast AvailableCursorModes bit
PORTAL_DIALOG_TIMEOUT_S = 180  # the user may take a while to pick a monitor
SESSION_RETRY_S = 3.0  # wait between attempts to reopen a lost screen-share session

# If the portal's stream is gone, WirePlumber must not link us to the default
# video source instead: that is a webcam, not the screen the user picked.
_PIPEWIRESRC_STREAM_PROPS = (
    'stream-properties="props,node.dont-fallback=(string)true,node.dont-reconnect=(string)true"'
)


def _resize_nearest(frame: np.ndarray, width: int, height: int) -> np.ndarray:
    """Nearest-neighbour resize, for frames a pipeline could not scale itself."""
    h, w = frame.shape[:2]
    if (w, h) == (width, height):
        return frame
    rows = np.arange(height) * h // height
    cols = np.arange(width) * w // width
    return frame[rows][:, cols]


def _scaled(size: int, factor: float) -> int:
    # Multiples of 4 keep RGB row strides 4-byte aligned (no padded strides)
    # and avoid odd-width caps that some converters refuse to link.
    return max(4, int(size * factor) & ~3)


class _MonitorStream:
    """One shared portal stream (usually a monitor) and its pipeline."""

    def __init__(
        self,
        node_id: int,
        position: tuple[int, int] | None = None,
        size: tuple[int, int] | None = None,
    ):
        self.node_id = node_id
        self.position = position  # (x, y) in the compositor's layout, if known
        self.source_size = size  # (w, h) from the portal, else the first frame
        self.target_size: tuple[int, int] | None = None  # None = unscaled
        self.pipeline: Gst.Pipeline | None = None
        self.appsink: GstApp.AppSink | None = None
        self.running = False
        self.scaled = False
        self.needs_restart = False
        self.active_config_key: str | None = None
        self.error_logged = False
        self.map_failures = 0
        self.lock = threading.Lock()
        self.latest_frame: np.ndarray | None = None

    def frame(self) -> np.ndarray | None:
        with self.lock:
            return self.latest_frame

    def describe(self) -> str:
        size = f"{self.source_size[0]}x{self.source_size[1]}" if self.source_size else "size ?"
        at = f" at {self.position[0]},{self.position[1]}" if self.position else ""
        return f"node {self.node_id} ({size}{at})"


class ScreenCapture:
    def __init__(
        self,
        scale_factor: float = 0.125,
        restore_token: str = "",
        max_fps: int = 30,
    ):
        self.scale_factor = scale_factor
        # Asked of the compositor, which then renders no frames we would drop
        self.max_fps = max_fps

        self._portal_session_handle: str | None = None
        self._portal_bus: Gio.DBusConnection | None = None
        # PipeWire connection handed out by the portal. It only exposes the
        # streams the user granted, so pipewiresrc can never fall back to
        # another video source (e.g. a webcam) if the screen stream goes away.
        self._pipewire_fd: int | None = None
        # Portal restore token for persistent screen-cast consent. When set,
        # the portal can skip the permission dialog on subsequent sessions.
        self._restore_token: str = restore_token or ""
        self._on_restore_token_changed = None
        self._reselect_requested = False
        self._session_lost = False  # the compositor ended our stream(s)
        self._retry_at = 0.0  # monotonic time before which no new session is tried
        if self._restore_token:
            timed_print(
                f"[capture] Loaded cached screen-share consent token "
                f"({len(self._restore_token)} chars) — should skip prompt"
            )
        else:
            timed_print("[capture] No cached consent token — permission dialog will be shown")

        self._streams: list[_MonitorStream] = []
        self._horizontal = True  # combine streams side by side (else stacked)
        self._logged_warnings: set[str] = set()
        self._runtime_failed_configs: set[str] = set()

        # Letterbox/pillarbox bars are ignored automatically, as in Hue Sync
        self._black_bar_detector = BlackBarDetector()
        # Part of the (combined) screen to sync with: x0, y0, x1, y1 in 0-1
        self.capture_area = (0.0, 0.0, 1.0, 1.0)
        # The whole combined screen of the latest frame, for the preview
        self.last_full_frame: np.ndarray | None = None
        self._cached: tuple | None = None  # (frames, capture area, result)

    def _update_target_sizes(self) -> None:
        """Decide what each stream scales to so the frames line up when combined.

        A single stream keeps its aspect ratio. Several streams are scaled to
        a common height (side by side) or width (stacked).
        """
        streams = self._streams
        if not streams or self.scale_factor >= 1.0 or any(s.source_size is None for s in streams):
            targets = [None] * len(streams)
        elif len(streams) == 1:
            w, h = streams[0].source_size
            targets = [(_scaled(w, self.scale_factor), _scaled(h, self.scale_factor))]
        elif self._horizontal:
            height = _scaled(max(s.source_size[1] for s in streams), self.scale_factor)
            targets = [(_scaled(w * height / h, 1.0), height) for w, h in (s.source_size for s in streams)]
        else:
            width = _scaled(max(s.source_size[0] for s in streams), self.scale_factor)
            targets = [(width, _scaled(h * width / w, 1.0)) for w, h in (s.source_size for s in streams)]

        for stream, target in zip(streams, targets):
            if stream.target_size != target:
                stream.target_size = target
                if stream.running:
                    # Restarted from capture(), on the thread that owns it
                    stream.needs_restart = True

    def request_new_selection(self) -> None:
        """Forget the remembered screen choice so the portal dialog shows again.

        Takes effect on the next capture: immediately while syncing, else at
        the next sync start.
        """
        self._reselect_requested = True
        if self._restore_token:
            self._restore_token = ""
            if self._on_restore_token_changed:
                try:
                    self._on_restore_token_changed("")
                except Exception as e:
                    timed_print(f"Error clearing restore token: {e}")

    @classmethod
    def pick_displays(cls) -> str:
        """Show the portal's monitor picker in a session of its own.

        Returns the restore token for the chosen monitors ("" if cancelled).
        Separate from any running capture, so the two cannot close each
        other's session. Blocks until answered: call from a worker thread.
        """
        tokens = []
        picker = cls()
        picker.set_on_restore_token_changed(tokens.append)
        picker._setup_portal_session()
        picker.stop_pipeline()
        return tokens[-1] if tokens else ""

    def set_restore_token(self, token: str) -> None:
        """Use (and persist) monitors chosen with pick_displays()."""
        self._restore_token = token
        if self._on_restore_token_changed:
            self._on_restore_token_changed(token)

    def _stop_stream(self, stream: _MonitorStream) -> None:
        if stream.pipeline:
            bus = stream.pipeline.get_bus()
            if bus:
                bus.remove_signal_watch()
            stream.pipeline.set_state(Gst.State.NULL)
            stream.pipeline = None
            stream.appsink = None
        stream.active_config_key = None
        stream.running = False
        stream.needs_restart = False
        with stream.lock:
            stream.latest_frame = None

    @property
    def restore_token(self) -> str:
        """Current portal restore token (for diagnostics)."""
        return self._restore_token

    def set_on_restore_token_changed(self, callback) -> None:
        """Register a callback invoked when the portal issues/refreshes a
        restore token. Signature: callback(token: str). Use it to persist the
        token so subsequent runs can skip the permission dialog.
        """
        self._on_restore_token_changed = callback

    def capture(self) -> np.ndarray | None:
        if self._reselect_requested:
            self._reselect_requested = False
            self.stop_pipeline()  # the next session shows the dialog again
        elif self._session_lost:
            # Like Hue Sync's "stream was stopped" recovery: start over with
            # a fresh session (the remembered choice skips the dialog).
            timed_print("[capture] Screen-share stream ended; reopening the session")
            self._session_lost = False
            self.stop_pipeline()

        if not self._streams:
            if time.monotonic() < self._retry_at:
                return None
            if not self._open_session():
                self._retry_at = time.monotonic() + SESSION_RETRY_S
                return None

        for stream in self._streams:
            if stream.needs_restart:
                self._stop_stream(stream)
            if not stream.running and not self._start_stream(stream):
                # Every pipeline failed: most likely the stream is gone
                self.stop_pipeline()
                self._retry_at = time.monotonic() + SESSION_RETRY_S
                return None

        frames = self._wait_for_frames(timeout=2.0)
        if frames is None:
            return None
        cached = self._cached
        if (
            cached
            and cached[1] == self.capture_area
            and len(cached[0]) == len(frames)
            and all(a is b for a, b in zip(cached[0], frames))
        ):
            return cached[2]  # no new frame since the last call
        full = self._combine(frames)
        result = self._process_image(self._crop_to_capture_area(full))
        self.last_full_frame = full
        self._cached = (frames, self.capture_area, result)
        return result

    def _crop_to_capture_area(self, screen: np.ndarray) -> np.ndarray:
        x0, y0, x1, y1 = self.capture_area
        if (x0, y0, x1, y1) == (0.0, 0.0, 1.0, 1.0):
            return screen
        height, width = screen.shape[:2]
        top, left = int(y0 * height), int(x0 * width)
        return screen[top : max(top + 4, int(y1 * height)), left : max(left + 4, int(x1 * width))]

    def _open_session(self) -> bool:
        if self._setup_portal_session():
            return True
        # A stale restore token (e.g. after a monitor/source change) can make
        # session setup fail silently. Retry once without it so the portal
        # shows a fresh permission dialog.
        if not self._restore_token:
            return False
        timed_print(
            "[capture] Cached consent token was rejected by portal "
            "(stale or revoked) — clearing cache and re-prompting"
        )
        self._restore_token = ""
        if self._on_restore_token_changed:
            try:
                self._on_restore_token_changed("")
            except Exception as e:
                timed_print(f"Error clearing restore token: {e}")
        return self._setup_portal_session()

    def _wait_for_frames(self, timeout: float) -> list[np.ndarray] | None:
        """Latest frame of every stream; waits only until each delivered its first."""
        start = time.time()
        while True:
            frames = [stream.frame() for stream in self._streams]
            if all(frame is not None for frame in frames):
                return frames
            if time.time() - start >= timeout:
                return None
            time.sleep(0.01)

    def _combine(self, frames: list[np.ndarray]) -> np.ndarray:
        if len(frames) == 1:
            return frames[0]
        parts = [frame[:, :, :3] for frame in frames]
        if self._horizontal:
            height = min(part.shape[0] for part in parts)
            parts = [
                _resize_nearest(part, max(1, part.shape[1] * height // part.shape[0]), height)
                for part in parts
            ]
            return np.hstack(parts)
        width = min(part.shape[1] for part in parts)
        parts = [
            _resize_nearest(part, width, max(1, part.shape[0] * width // part.shape[1])) for part in parts
        ]
        return np.vstack(parts)

    def _process_image(self, screen: np.ndarray) -> np.ndarray:
        screen = self._black_bar_detector.crop(screen)
        if self.scale_factor < 1.0 and not all(s.scaled for s in self._streams):
            new_h = max(1, int(screen.shape[0] * self.scale_factor))
            new_w = max(1, int(screen.shape[1] * self.scale_factor))
            screen = _resize_nearest(screen, new_w, new_h)
        return screen

    def _setup_portal_session(self) -> bool:
        """Ask the ScreenCast portal for monitors and open their streams.

        Follows the portal spec: persist_mode/restore_token go to
        SelectSources, and the streams are read through the restricted
        PipeWire remote from OpenPipeWireRemote rather than the global
        PipeWire socket.
        """
        try:
            self._portal_bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)

            results = self._screencast(
                "CreateSession",
                [],
                {"session_handle_token": GLib.Variant("s", f"glowcast_{uuid.uuid4().hex}")},
            )
            if not results or "session_handle" not in results:
                return False
            self._portal_session_handle = results["session_handle"]

            select_options = {
                "types": GLib.Variant("u", 1),  # monitors
                # Several monitors can be shared and are captured together
                "multiple": GLib.Variant("b", True),
                # 2 = keep consent until revoked, so Start can return a
                # restore_token that skips the dialog next time.
                "persist_mode": GLib.Variant("u", 2),
            }
            if self._portal_cursor_modes() & CURSOR_MODE_HIDDEN:
                select_options["cursor_mode"] = GLib.Variant("u", CURSOR_MODE_HIDDEN)
            if self._restore_token:
                select_options["restore_token"] = GLib.Variant("s", self._restore_token)
                timed_print(
                    "[capture] Restoring cached screen-share consent "
                    f"(token length {len(self._restore_token)}) — "
                    "dialog should be skipped"
                )
            else:
                timed_print("[capture] Requesting screen-share consent — dialog will be shown")
            session = [("o", self._portal_session_handle)]
            if self._screencast("SelectSources", session, select_options) is None:
                return False

            results = self._screencast("Start", [*session, ("s", "")], {}, timeout_s=PORTAL_DIALOG_TIMEOUT_S)
            streams = (results or {}).get("streams") or []
            if not streams:
                return False

            self._pipewire_fd = self._open_pipewire_remote()
            if self._pipewire_fd is None:
                return False

            self._set_streams(streams)
            timed_print(
                f"Portal session started with {len(self._streams)} stream(s): "
                + ", ".join(s.describe() for s in self._streams)
            )

            new_token = results.get("restore_token") or ""
            if new_token:
                timed_print(
                    f"[capture] Screen-share consent cached "
                    f"(token length {len(new_token)}) — "
                    "will skip prompt on next sync"
                )
            else:
                timed_print("[capture] Portal did not issue a consent token; it will ask again next sync")
            if new_token and new_token != self._restore_token:
                self._restore_token = new_token
                if self._on_restore_token_changed:
                    try:
                        self._on_restore_token_changed(new_token)
                    except Exception as e:
                        timed_print(f"Error persisting restore token: {e}")
            return True

        except Exception as e:
            timed_print(f"Failed to setup portal session: {e}")

        self._close_portal_session()
        return False

    def _set_streams(self, portal_streams: list) -> None:
        """Create a stream per shared source, ordered as the monitors are laid out."""
        streams = [
            _MonitorStream(node_id, props.get("position"), props.get("size"))
            for node_id, props in portal_streams
        ]
        if len(streams) > 1 and all(s.position for s in streams):
            xs = [s.position[0] for s in streams]
            ys = [s.position[1] for s in streams]
            self._horizontal = max(xs) - min(xs) >= max(ys) - min(ys)
            streams.sort(key=lambda s: s.position if self._horizontal else s.position[::-1])
        else:
            self._horizontal = True
        self._streams = streams
        # Knowing the sizes up front lets the first pipelines scale right
        # away instead of restarting once the first frames reveal them.
        self._update_target_sizes()

    def _screencast(self, method: str, args: list, options: dict, timeout_s: int = 30):
        return portal.request(self._portal_bus, SCREENCAST_IFACE, method, args, options, timeout_s)

    def _portal_cursor_modes(self) -> int:
        """Bitmask of cursor modes the ScreenCast portal supports (0 if unknown)."""
        try:
            reply = self._portal_bus.call_sync(
                portal.BUS_NAME,
                portal.OBJECT_PATH,
                "org.freedesktop.DBus.Properties",
                "Get",
                GLib.Variant("(ss)", (SCREENCAST_IFACE, "AvailableCursorModes")),
                GLib.VariantType("(v)"),
                Gio.DBusCallFlags.NONE,
                -1,
                None,
            )
            return reply.unpack()[0]
        except GLib.Error:
            return 0

    def _open_pipewire_remote(self) -> int | None:
        """Get the portal's restricted PipeWire connection as a file descriptor."""
        try:
            reply, fd_list = self._portal_bus.call_with_unix_fd_list_sync(
                portal.BUS_NAME,
                portal.OBJECT_PATH,
                SCREENCAST_IFACE,
                "OpenPipeWireRemote",
                GLib.Variant("(oa{sv})", (self._portal_session_handle, {})),
                GLib.VariantType("(h)"),
                Gio.DBusCallFlags.NONE,
                -1,
                None,
                None,
            )
            return fd_list.get(reply.unpack()[0])
        except GLib.Error as e:
            timed_print(f"Portal OpenPipeWireRemote failed: {e.message}")
            return None

    def _get_pipeline_configs(self, stream: _MonitorStream) -> list[str]:
        """Candidate pipelines for a stream, cheapest first.

        GPU paths first, like Hue Sync on macOS/Windows: the frame stays a
        DMA-BUF on the GPU, is scaled there, and only the small result is
        copied to memory. VA-API is cheapest (~7x less CPU than scaling on
        the CPU), OpenGL a close second. The CPU path scales in the source
        format and converts only the small frame; without GPU elements,
        pipewiresrc negotiates shared memory, which every compositor offers.
        The frame-rate cap is negotiated with the compositor, so frames
        above the sync rate are never rendered or copied at all.
        """
        has = {
            name: Gst.ElementFactory.find(name) is not None
            for name in (
                "vapostproc",
                "glupload",
                "glcolorconvert",
                "glcolorscale",
                "gldownload",
                "videoscale",
                "videoconvert",
            )
        }
        rate = f"max-framerate={self.max_fps}/1"
        sink = "appsink name=sink emit-signals=true drop=true max-buffers=1 sync=false"
        # The portal hands out a node *id*: that is what (deprecated) "path"
        # takes. "target-object" expects a name or serial, and an id there
        # silently matches nothing and falls back to the default source.
        src = (
            f"pipewiresrc fd={self._pipewire_fd} path={stream.node_id} "
            f"{_PIPEWIRESRC_STREAM_PROPS} do-timestamp=true"
        )
        dmabuf = f"{src} ! video/x-raw(memory:DMABuf),{rate}"
        shm = f"{src} ! video/x-raw,{rate}"

        configs = []
        if stream.target_size:
            w, h = stream.target_size
            if has["vapostproc"]:
                # BGRA, VA's native order: some drivers (Mesa radeonsi) write
                # "RGBA" output with the bytes reversed, turning blue into red.
                configs.append(
                    f"{dmabuf} ! vapostproc ! video/x-raw,format=BGRA,width={w},height={h} ! {sink}"
                )
            if all(has[e] for e in ("glupload", "glcolorconvert", "glcolorscale", "gldownload")):
                configs.append(
                    f"{dmabuf} ! glupload ! glcolorconvert ! glcolorscale ! "
                    f"video/x-raw(memory:GLMemory),format=RGBA,width={w},height={h} ! "
                    f"gldownload ! video/x-raw,format=RGBA ! {sink}"
                )
            if has["videoscale"] and has["videoconvert"]:
                configs.append(
                    f"{shm} ! videoscale ! video/x-raw,width={w},height={h} ! "
                    f"videoconvert ! video/x-raw,format=RGB ! {sink}"
                )
        if has["videoconvert"]:
            # Size not known yet (or no scaling): the first frame reveals it
            configs.append(f"{shm} ! videoconvert ! video/x-raw,format=RGB ! {sink}")
        return configs

    @staticmethod
    def _config_key(pipeline_str: str) -> str:
        """Identify a pipeline config independent of node id and dimensions,
        so runtime failures can be remembered across restarts."""
        key = re.sub(r"(fd|path|target-object)=\d+", r"\1=*", pipeline_str)
        key = re.sub(r"width=\d+", "width=*", key)
        key = re.sub(r"height=\d+", "height=*", key)
        return key

    @staticmethod
    def _describe_config(pipeline_str: str) -> str:
        """Name of the first processing element (skipping source and caps filters)."""
        for element in pipeline_str.split(" ! ")[1:]:
            if not element.startswith("video/x-raw"):
                return element.split()[0]
        return "unknown"

    def _start_stream(self, stream: _MonitorStream) -> bool:
        if stream.pipeline:
            self._stop_stream(stream)

        configs = self._get_pipeline_configs(stream)
        if not configs:
            timed_print("No suitable GStreamer pipeline configuration found")
            timed_print("Need vapostproc, the GL elements, or videoscale + videoconvert")
            return False

        target_w, _target_h = stream.target_size or (None, None)

        for i, pipeline_str in enumerate(configs):
            config_key = self._config_key(pipeline_str)
            if config_key in self._runtime_failed_configs:
                continue
            try:
                stream.pipeline = Gst.parse_launch(pipeline_str)
                stream.appsink = stream.pipeline.get_by_name("sink")
                stream.appsink.connect("new-sample", self._on_new_sample, stream)

                bus = stream.pipeline.get_bus()
                bus.add_signal_watch()
                bus.connect("message::error", self._on_pipeline_error, stream)
                bus.connect("message::warning", self._on_pipeline_warning)

                ret = stream.pipeline.set_state(Gst.State.PLAYING)
                if ret == Gst.StateChangeReturn.FAILURE:
                    desc = self._describe_config(pipeline_str)
                    timed_print(f"Pipeline config {i + 1} failed (converter: {desc})")
                    stream.pipeline.set_state(Gst.State.NULL)
                    stream.pipeline = None
                    continue

                stream.scaled = target_w is not None and f"width={target_w}" in pipeline_str
                stream.running = True
                stream.error_logged = False
                stream.map_failures = 0
                stream.active_config_key = config_key
                desc = self._describe_config(pipeline_str)
                scale_info = (
                    f", scaled to {stream.target_size[0]}x{stream.target_size[1]}" if stream.scaled else ""
                )
                timed_print(
                    f"GStreamer capture pipeline started for node {stream.node_id} "
                    f"(converter: {desc}{scale_info})"
                )
                return True

            except Exception as e:
                timed_print(f"Pipeline config {i + 1} exception: {e}")
                if stream.pipeline:
                    stream.pipeline.set_state(Gst.State.NULL)
                    stream.pipeline = None
                continue

        stream.scaled = False
        timed_print(f"All pipeline configurations failed for node {stream.node_id}")
        return False

    def _on_pipeline_error(self, bus, message, stream: _MonitorStream):
        err, debug = message.parse_error()
        if not stream.error_logged:
            timed_print(f"GStreamer pipeline error: {err.message}")
            timed_print(f"GStreamer debug info: {debug}")
            stream.error_logged = True
        if "target not found" in err.message:
            # The portal stream is gone, not the pipeline's fault
            self._session_lost = True
        elif stream.active_config_key:
            self._runtime_failed_configs.add(stream.active_config_key)
            timed_print("Marking pipeline config as failed; will fall back to next converter on restart")
        self._stop_stream(stream)

    def _on_pipeline_warning(self, bus, message):
        warn, debug = message.parse_warning()
        # Some elements repeat the same warning for every frame
        if warn.message in self._logged_warnings:
            return
        self._logged_warnings.add(warn.message)
        timed_print(f"GStreamer pipeline warning: {warn.message}")
        if debug:
            timed_print(f"GStreamer debug info: {debug}")

    @staticmethod
    def _extract_pixel_rows(data: bytes, width: int, height: int, bpp: int, stride: int | None) -> np.ndarray:
        """Return a (height, width*bpp) uint8 array, stripping row padding.

        GStreamer may pad each row to an alignment boundary (e.g. 4 bytes),
        so the buffer stride can exceed width*bpp. The real stride comes from
        GstVideoMeta when available, otherwise it is inferred from the buffer
        size.
        """
        row_bytes = width * bpp
        if not stride or stride < row_bytes:
            if len(data) == height * row_bytes:
                stride = row_bytes
            else:
                stride = len(data) // height if height else row_bytes
        if stride < row_bytes or len(data) < stride * (height - 1) + row_bytes:
            raise ValueError(
                f"Buffer too small: {len(data)} bytes for {width}x{height} with bpp={bpp} (stride={stride})"
            )

        arr = np.frombuffer(data, dtype=np.uint8)
        if stride == row_bytes:
            return arr[: height * row_bytes].reshape(height, row_bytes)

        # Padded rows: build a strided view (handles a possibly unpadded
        # final row) and copy to detach from the mapped buffer.
        rows = np.lib.stride_tricks.as_strided(arr, shape=(height, row_bytes), strides=(stride, 1))
        return rows.copy()

    def _on_new_sample(self, appsink, stream: _MonitorStream) -> Gst.FlowReturn:
        try:
            sample = appsink.emit("pull-sample")
            if not sample:
                return Gst.FlowReturn.OK

            buffer = sample.get_buffer()
            caps = sample.get_caps()

            struct = caps.get_structure(0)
            width = struct.get_value("width")
            height = struct.get_value("height")
            fmt = struct.get_value("format") if struct.has_field("format") else None

            if not stream.scaled and stream.source_size != (width, height):
                stream.source_size = (width, height)
                self._update_target_sizes()

            stride = None
            try:
                meta = GstVideo.buffer_get_video_meta(buffer)
                if meta and meta.n_planes >= 1:
                    stride = meta.stride[0]
            except Exception:
                stride = None

            success, map_info = buffer.map(Gst.MapFlags.READ)
            if not success:
                stream.map_failures += 1
                if stream.map_failures == 1:
                    timed_print(f"Failed to map buffer (format={fmt}, {width}x{height})")
                if stream.map_failures >= 10 and stream.active_config_key:
                    # Unreadable frames: give up on this pipeline and let
                    # capture() restart the stream with the next one.
                    timed_print("Frames cannot be read; falling back to the next converter")
                    self._runtime_failed_configs.add(stream.active_config_key)
                    stream.needs_restart = True
                return Gst.FlowReturn.OK
            stream.map_failures = 0

            try:
                # Only these formats are ever negotiated (see _get_pipeline_configs)
                bpp = 3 if fmt == "RGB" else 4
                rows = self._extract_pixel_rows(bytes(map_info.data), width, height, bpp, stride)
                frame = rows.reshape(height, width, bpp)
                if fmt in ("BGRA", "BGRx"):
                    frame = frame[:, :, [2, 1, 0]]
                with stream.lock:
                    stream.latest_frame = frame
            finally:
                buffer.unmap(map_info)

            return Gst.FlowReturn.OK

        except Exception as e:
            timed_print(f"Error processing frame: {e}")
            return Gst.FlowReturn.OK

    def _close_portal_session(self):
        if self._portal_session_handle and self._portal_bus:
            try:
                self._portal_bus.call_sync(
                    portal.BUS_NAME,
                    self._portal_session_handle,
                    "org.freedesktop.portal.Session",
                    "Close",
                    None,
                    None,
                    Gio.DBusCallFlags.NONE,
                    -1,
                    None,
                )
                timed_print("Portal session closed")
            except GLib.Error as e:
                timed_print(f"Error closing portal session (may already be closed): {e.message}")
        if self._pipewire_fd is not None:
            with contextlib.suppress(OSError):
                os.close(self._pipewire_fd)
            self._pipewire_fd = None
        self._portal_session_handle = None
        self._portal_bus = None

    def stop_pipeline(self):
        for stream in self._streams:
            self._stop_stream(stream)
        self._streams = []
        self._runtime_failed_configs.clear()
        self._cached = None
        self.last_full_frame = None
        timed_print("GStreamer pipeline stopped")
        self._close_portal_session()

    def __del__(self):
        self.stop_pipeline()
