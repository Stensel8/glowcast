"""Hue Entertainment streaming: colour updates over DTLS 1.2 (PSK) to UDP port 2100.

Python has no DTLS, so the session runs through `openssl s_client` (part of
every desktop and the GNOME runtime); its stdin carries HueStream v2
messages: a 52-byte header plus 7 bytes per channel (id, x, y, brightness).
"""

import collections
import struct
import subprocess
import threading
import time
from typing import TYPE_CHECKING

from glowcast.utils.logging import timed_print

if TYPE_CHECKING:
    from glowcast.hue_bridge import HueBridge


class HueStreamProtocol:
    HEADER = b"HueStream\x02\x00"  # protocol name and version 2.0
    COLORSPACE_XY = 0x01
    PORT = 2100
    HEADER_SIZE = 52
    CHANNEL_SIZE = 7
    HANDSHAKE_TIMEOUT = 5.0  # seconds; a wrong PSK stalls instead of failing
    RECONNECT_INTERVAL = 3.0  # seconds between attempts after a dropped session
    RESTART_DELAY = 1.0  # pause between stopping and restarting the area on a retry
    TAKEOVER_WINDOW_S = 2.0  # a drop this soon after sending was done to us, not an idle timeout


class EntertainmentStream:
    """One streaming session to an entertainment area."""

    def __init__(
        self,
        bridge_ip: str,
        app_key: str,
        client_key: str,
        entertainment_config_id: str,
        connection_timeout: float = 0.5,
    ):
        self.bridge_ip = bridge_ip
        self.app_key = app_key
        self.client_key = client_key  # the PSK
        self.entertainment_config_id = entertainment_config_id
        self._connection_timeout = connection_timeout  # lets the bridge open its DTLS port
        self.last_error = ""  # why the last connect() failed
        self.in_use_by_other = False  # the last connect() found another app streaming
        self._application_id: str | None = None  # the PSK identity
        self._openssl_proc: subprocess.Popen | None = None
        self._openssl_log: collections.deque[str] = collections.deque(maxlen=20)
        self._connected = False
        self._sequence = 0
        self._lock = threading.Lock()
        self._next_reconnect = 0.0
        self._positions: dict[int, dict] = {}  # channel id -> {"x", "y", "z"}
        self._message = bytearray()
        self.area_started = False  # we started streaming on the bridge and haven't stopped it
        self.last_colors: dict[int, tuple[tuple[float, float], int]] = {}  # what was sent last
        self.taken_over = False  # another app (e.g. the Hue app's "stop") took the area from us
        self._last_send = 0.0

    # --- session ----------------------------------------------------------------

    def connect(self, bridge: "HueBridge", take_over: bool = False) -> bool:
        """Start the area and open the DTLS session.

        On failure last_error says why, and in_use_by_other tells whether
        another app streams to the area; take_over stops that app's stream
        first (Hue Sync's "Start anyway").
        """
        self.last_error = ""
        self.in_use_by_other = False
        self.taken_over = False
        # A previous session may have died without disconnect(); don't leave
        # its openssl process behind.
        self._connected = False
        self._stop_openssl()
        try:
            config = bridge.get_entertainment_configuration(self.entertainment_config_id)
            if not config:
                self.last_error = "Entertainment area not found on the bridge"
                return False
            self._read_channels(config)
            if not self._positions:
                self.last_error = "The entertainment area has no lights"
                return False
            self._application_id = bridge.get_application_id() or self.app_key

            if self._in_use_by_other_app(config):
                if not take_over:
                    self.in_use_by_other = True
                    return False
                timed_print("Taking over the entertainment area from the other app")
                bridge.deactivate_entertainment_streaming(self.entertainment_config_id)
                time.sleep(HueStreamProtocol.RESTART_DELAY)

            if not self._start_area(bridge):
                return False
            if not self._open_dtls():
                # Usually the bridge still holds an earlier session (Glowcast was
                # killed without stopping it): restarting the area clears that.
                timed_print("DTLS handshake failed; restarting the area and retrying")
                bridge.deactivate_entertainment_streaming(self.entertainment_config_id)
                time.sleep(HueStreamProtocol.RESTART_DELAY)
                if not (self._start_area(bridge) and self._open_dtls()):
                    bridge.deactivate_entertainment_streaming(self.entertainment_config_id)
                    return False
            self._connected = True
            timed_print(f"Entertainment stream connected with {len(self._positions)} channels")
            return True
        except Exception as e:
            timed_print(f"Error connecting entertainment stream: {e}")
            self.last_error = f"Unexpected error: {e}"
            return False

    def reconnect_if_dropped(self, bridge: "HueBridge") -> bool:
        """Re-open a session the bridge ended, at most every few seconds.

        The bridge drops a session after ~10 s without data (e.g. while the
        user picks a monitor). A session that ends while we were sending was
        ended on purpose, by the Hue app's "stop" or another app streaming:
        then the lights are theirs, taken_over is set and we don't grab them
        back. True only when this call reconnected, so callers know to
        refresh anything derived from the channel layout.
        """
        now = time.monotonic()
        if self.is_connected() or self.taken_over or now < self._next_reconnect:
            return False
        if now - self._last_send < HueStreamProtocol.TAKEOVER_WINDOW_S and self._area_taken(bridge):
            self.taken_over = True
            self._connected = self.area_started = False  # not ours to stop any more
            self._stop_openssl()
            self.last_error = "The lights were taken over by another app"
            timed_print("Entertainment area taken over by another app; not reconnecting")
            return False
        self._next_reconnect = now + HueStreamProtocol.RECONNECT_INTERVAL
        timed_print("Entertainment stream dropped; reconnecting...")
        if self.connect(bridge):
            return True
        timed_print(f"Reconnect failed: {self.last_error}")
        return False

    def _area_taken(self, bridge: "HueBridge") -> bool:
        config = bridge.get_entertainment_configuration(self.entertainment_config_id)
        if not config:
            return False  # can't tell: a reconnect attempt will find out
        streamer = (config.get("active_streamer") or {}).get("rid")
        return config.get("status") != "active" or bool(streamer and streamer != self._application_id)

    def disconnect(self, bridge: "HueBridge") -> None:
        self._connected = False
        self.area_started = False
        self._stop_openssl()
        bridge.deactivate_entertainment_streaming(self.entertainment_config_id)
        timed_print("Entertainment stream disconnected")

    def is_connected(self) -> bool:
        return self._connected and self._openssl_proc is not None and self._openssl_proc.poll() is None

    def get_channel_positions(self) -> dict[int, dict]:
        """Channel id -> position: x -1..1 (left to right), y (front/back), z -1..1 (floor to ceiling)."""
        return dict(self._positions)

    def _read_channels(self, config: dict) -> None:
        positions = {}
        for channel in config.get("channels", []):
            if (channel_id := channel.get("channel_id")) is not None:
                position = channel.get("position", {})
                positions[channel_id] = {axis: position.get(axis, 0) for axis in "xyz"}
        if positions != self._positions:  # log the layout once, not on every connect
            for channel_id, position in positions.items():
                timed_print(f"  Channel {channel_id}: {position}")
        self._positions = positions
        self._message = bytearray(
            HueStreamProtocol.HEADER_SIZE + HueStreamProtocol.CHANNEL_SIZE * len(self._positions)
        )
        self._message[0:11] = HueStreamProtocol.HEADER
        self._message[14] = HueStreamProtocol.COLORSPACE_XY
        self._message[16:52] = self.entertainment_config_id.encode("ascii")

    def _in_use_by_other_app(self, config: dict) -> bool:
        """Another app streams to the area (an earlier Glowcast session doesn't count)."""
        streamer = (config.get("active_streamer") or {}).get("rid")
        if config.get("status") != "active" or not streamer:
            return False
        if self._application_id == self.app_key or streamer == self._application_id:
            return False  # our own id unknown, or our own stale session: just restart
        timed_print(f"Entertainment area is in use by another application ({streamer})")
        self.last_error = "The area is in use by another app (Hue app, Sync Box, Hue Sync)"
        return True

    def _start_area(self, bridge: "HueBridge") -> bool:
        if not bridge.activate_entertainment_streaming(self.entertainment_config_id):
            self.last_error = "The bridge refused to start the entertainment area"
            return False
        self.area_started = True
        time.sleep(self._connection_timeout)
        return True

    # --- openssl ------------------------------------------------------------------

    def _open_dtls(self) -> bool:
        command = [
            "openssl", "s_client", "-dtls1_2",
            "-psk_identity", self._application_id or self.app_key,
            "-psk", self.client_key,
            "-cipher", "PSK-AES128-GCM-SHA256:PSK-CHACHA20-POLY1305",
            "-connect", f"{self.bridge_ip}:{HueStreamProtocol.PORT}",
            "-quiet", "-state",
        ]  # fmt: skip
        timed_print(f"Starting DTLS connection to {self.bridge_ip}:{HueStreamProtocol.PORT}")
        try:
            # stdout stays block-buffered while openssl runs, so handshake
            # progress is read from the -state trace on (unbuffered) stderr.
            # Its own session: Ctrl+C in a terminal must not kill it before
            # Glowcast has stopped the stream on the bridge.
            self._openssl_proc = subprocess.Popen(
                command,
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                start_new_session=True,
            )
        except FileNotFoundError:
            self.last_error = "The openssl command is missing"
            return False
        return self._wait_for_handshake()

    def _wait_for_handshake(self) -> bool:
        """Wait for openssl to report the bridge's Finished message.

        A bad PSK makes the handshake stall rather than fail, so a timeout
        counts as failure too.
        """
        proc = self._openssl_proc
        handshake_done, wake = threading.Event(), threading.Event()
        self._openssl_log.clear()
        threading.Thread(
            target=self._drain_stderr, args=(proc, handshake_done, wake), daemon=True, name="OpenSSL"
        ).start()
        wake.wait(HueStreamProtocol.HANDSHAKE_TIMEOUT)
        if handshake_done.is_set() and proc.poll() is None:
            timed_print("DTLS connection established")
            return True
        self.last_error = (
            "The bridge did not answer the secure connection. If this keeps happening, "
            "set up the bridge again in Settings."
            if proc.poll() is None
            else "The bridge rejected the DTLS connection"
        )
        timed_print(f"DTLS handshake failed: {self.last_error}")
        for line in self._openssl_log:
            timed_print(f"  openssl: {line}")
        self._stop_openssl()
        return False

    def _drain_stderr(self, proc, handshake_done: threading.Event, wake: threading.Event) -> None:
        """Read openssl's stderr until it exits (so the pipe never fills up)."""
        for raw in proc.stderr:
            if line := raw.decode(errors="replace").strip():
                self._openssl_log.append(line)
                if "read finished" in line:
                    handshake_done.set()
                    wake.set()
        wake.set()  # exited: wake the waiter instead of letting it time out

    def _stop_openssl(self) -> None:
        proc, self._openssl_proc = self._openssl_proc, None
        if not proc:
            return
        try:
            proc.stdin.close()
            proc.terminate()
            proc.wait(timeout=2)
        except (OSError, subprocess.TimeoutExpired):
            proc.kill()

    # --- sending ------------------------------------------------------------------

    def send_colors_xy(self, colors: dict[int, tuple[tuple[float, float], int]]) -> None:
        """Send ((x, y), brightness 0-254) per channel; missing channels go dark."""
        if not self.is_connected():
            return
        self.last_colors = colors
        with self._lock:
            message = self._message
            message[11] = self._sequence
            offset = HueStreamProtocol.HEADER_SIZE
            for channel_id in sorted(self._positions):
                (x, y), brightness = colors.get(channel_id, ((0.0, 0.0), 0))
                struct.pack_into(
                    ">BHHH",
                    message,
                    offset,
                    channel_id,
                    int(min(1.0, max(0.0, x)) * 65535),
                    int(min(1.0, max(0.0, y)) * 65535),
                    min(254, max(0, brightness)) * 257,
                )
                offset += HueStreamProtocol.CHANNEL_SIZE
            try:
                self._openssl_proc.stdin.write(message)
                self._openssl_proc.stdin.flush()
                self._sequence = (self._sequence + 1) % 256
                self._last_send = time.monotonic()
            except (OSError, ValueError, AttributeError) as e:
                timed_print(f"DTLS connection lost: {e}")
                self._connected = False
