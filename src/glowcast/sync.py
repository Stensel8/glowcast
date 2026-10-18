"""Video and games mode: the screen -> lights loop on a worker thread.

Screen frames arrive at up to CAPTURE_FPS; lights are updated SYNC_FPS times a
second, gliding between frames (like Hue Sync's interpolation), which keeps
fades smooth and lets the bridge drop a packet without anyone noticing.
"""

import contextlib
import queue
import threading
import time
from collections import deque

import numpy as np

from glowcast.colors import VideoEngine, srgb_to_linear, with_background_light
from glowcast.music import AudioListener
from glowcast.presets import GAME_PRESETS, VIDEO_PRESETS
from glowcast.utils.logging import timed_print

SYNC_FPS = 50  # light updates per second
CAPTURE_FPS = 30  # asked of the compositor
PREVIEW_INTERVAL_S = 0.1
BACKGROUND_LIGHT = 30  # brightness (of 254) kept with background lighting on


class SyncController:
    STOP_TIMEOUT_S = 3.0  # how long stop() waits for the loop (opening a screen share can take a while)

    def __init__(self, bridge, capture, zone_processor, settings, entertainment_stream=None):
        self.bridge = bridge
        self.capture = capture
        self.zone_processor = zone_processor
        self.settings = settings  # the SettingsManager: read live every update
        self.entertainment_stream = entertainment_stream
        self.mode = "video"  # or "games"; switching applies immediately

        self.running = False
        self._generation = 0  # each start() gets a new one; an outdated loop just ends
        self.thread: threading.Thread | None = None
        self.queue: queue.Queue = queue.Queue(maxsize=100)
        self._engine = VideoEngine()
        self._audio: AudioListener | None = None
        self._delayed: deque = deque()
        self._screen: np.ndarray | None = None
        self._zones: dict = {}
        self._zone_channel: dict[str, int] = {}
        self._channel_x: dict[int, float] = {}  # left/right position, for stereo audio effects
        # Channels no screen zone maps to (e.g. lights left at the origin in
        # the Hue app); they follow the whole screen instead.
        self._unmapped_channels: list[int] = []
        self._logged_mapping: tuple | None = None
        self._stats = {"fps": 0.0}

    def start(self) -> None:
        if self.running:
            return
        stream = self.entertainment_stream
        if stream and stream.is_connected():
            self._build_zone_channel_mapping()
        else:
            timed_print("Warning: entertainment stream not connected")
        self._engine.reset()
        self._delayed.clear()
        self.running = True
        self._generation += 1
        self.thread = threading.Thread(
            target=self._loop, args=(self._generation,), daemon=True, name="SyncLoop"
        )
        self.thread.start()

    def stop(self) -> None:
        if not self.running:
            return
        self.running = False
        if self.thread:
            # The loop closes the screen share itself once its last capture
            # returns, so a session that is still opening is never left behind.
            self.thread.join(timeout=self.STOP_TIMEOUT_S)
            if self.thread.is_alive():
                timed_print("Video sync is still finishing; it ends the screen share when done")

    def is_running(self) -> bool:
        return self.running

    def get_status(self) -> tuple | None:
        try:
            return self.queue.get_nowait()
        except queue.Empty:
            return None

    def get_stats(self) -> dict:
        return dict(self._stats)

    def _queue_status(self, status_type: str, message, data=None) -> None:
        with contextlib.suppress(queue.Full):
            self.queue.put_nowait((status_type, message, data))

    def _build_zone_channel_mapping(self) -> None:
        """Give every screen zone to the light nearest to it.

        Entertainment positions: x from -1 (left) to 1 (right), z from -1
        (floor) to 1 (ceiling); zones are placed on the same square.
        """
        positions = self.entertainment_stream.get_channel_positions()
        self._zone_channel = {}
        if not positions:
            self._unmapped_channels = []
            return
        for zone_id in self.zone_processor.zone_ids():
            x, z = self.zone_processor.position(zone_id)
            self._zone_channel[zone_id] = min(
                positions,
                key=lambda c: (positions[c].get("x", 0) - x) ** 2 + (positions[c].get("z", 0) - z) ** 2,
            )
        self._channel_x = {c: float(p.get("x", 0)) for c, p in positions.items()}
        mapped = set(self._zone_channel.values())
        unmapped = sorted(c for c in positions if c not in mapped)
        if (mapped, unmapped) == self._logged_mapping:
            self._unmapped_channels = unmapped
            return  # same layout as last time: nothing new to log
        self._logged_mapping = (mapped, unmapped)
        self._unmapped_channels = unmapped
        timed_print(f"Zone-channel mapping: {len(self._zone_channel)} zones to {len(mapped)} channels")
        if self._unmapped_channels:
            timed_print(
                f"Channels {self._unmapped_channels} match no screen edge; "
                "they will show the average screen colour"
            )

    def _loop(self, generation: int) -> None:
        frame_times: deque = deque(maxlen=SYNC_FPS)
        last = time.monotonic()
        next_preview = 0.0
        while self.running and generation == self._generation:
            started = time.monotonic()
            try:
                zones = self._update(started - last)
                if zones and started >= next_preview:
                    next_preview = started + PREVIEW_INTERVAL_S
                    self._queue_status(
                        "status", "syncing", {"zones": zones, "frame": self.capture.last_full_frame}
                    )
            except Exception as e:
                timed_print(f"Sync loop error: {e}")
                self._queue_status("error", str(e))
                time.sleep(1)
            last = started
            time.sleep(max(0.0, 1.0 / SYNC_FPS - (time.monotonic() - started)))
            frame_times.append(time.monotonic())
            if len(frame_times) > 1:
                self._stats["fps"] = (len(frame_times) - 1) / (frame_times[-1] - frame_times[0])
        if generation != self._generation:
            return  # a newer loop took over the capture (and its screen share)
        self._set_audio(False)
        self.capture.stop_pipeline()  # ends the screen-share session
        self._queue_status("status", "stopped")

    def _update(self, dt: float) -> dict | None:
        """One light update; returns the zone colours for the preview."""
        settings = self.settings.video_settings(self.mode)
        self.capture.capture_area = tuple(self.settings.capture.capture_area)
        screen = self.capture.capture()
        if screen is None:
            return None
        if screen is not self._screen:  # capture() repeats a frame until a new one arrives
            self._screen, self._zones = screen, self.zone_processor.process_image(screen)
        zones = self._zones
        if not zones:
            return None

        stream = self.entertainment_stream
        if not stream:
            return zones
        if not stream.is_connected():
            if not stream.reconnect_if_dropped(self.bridge):
                return zones
            self._build_zone_channel_mapping()

        presets = GAME_PRESETS if self.mode == "games" else VIDEO_PRESETS
        colors = self._engine.process(
            self._channel_colors(zones), presets[settings.intensity], dt, self.settings.lights.brightness
        )
        colors = self._effects(colors, settings)
        colors = self._delay(colors, self.settings.capture.sync_delay_ms / 1000)
        if colors:
            stream.send_colors_xy(colors)
        return zones

    def _channel_colors(self, zones: dict[str, np.ndarray]) -> dict[int, np.ndarray]:
        """Average each light's zones as light: in linear RGB."""
        ids = list(zones)
        linear = srgb_to_linear(np.array([zones[z] for z in ids]) / 255.0)
        per_channel: dict[int, list[np.ndarray]] = {}
        for zone_id, color in zip(ids, linear):
            channel = self._zone_channel.get(zone_id)
            if channel is not None:
                per_channel.setdefault(channel, []).append(color)
        colors = {channel: np.mean(values, axis=0) for channel, values in per_channel.items()}
        if self._unmapped_channels:
            screen = linear.mean(axis=0)
            colors.update(dict.fromkeys(self._unmapped_channels, screen))
        return colors

    def _effects(self, colors: dict, settings) -> dict:
        """Use audio for effect, and background lighting."""
        self._set_audio(settings.use_audio)
        if self._audio:
            self._audio.intensity = settings.intensity
            stereo = self.settings.music.localization
            peak = 254 * self.settings.lights.brightness
            for channel, (xy, brightness) in colors.items():
                factor = self._audio.gain(self._channel_x.get(channel, 0.0), stereo)
                colors[channel] = (xy, round(min(peak, brightness * factor)))
        if settings.background_light:
            floor = round(min(BACKGROUND_LIGHT, 254 * self.settings.lights.brightness))
            colors = {c: with_background_light(color, floor) for c, color in colors.items()}
        return colors

    def _set_audio(self, enabled: bool) -> None:
        if enabled and self._audio is None:
            listener = AudioListener(self.settings.music.audio_device)
            if listener.start():
                self._audio = listener
            else:
                timed_print(f"Use audio for effect: {listener.last_error}")
                settings = self.settings.video_settings(self.mode)
                settings.use_audio = False
        elif not enabled and self._audio is not None:
            self._audio.stop()
            self._audio = None

    def _delay(self, colors: dict, delay_s: float) -> dict | None:
        """Hold colours back by delay_s, for displays that run behind."""
        if delay_s <= 0:
            self._delayed.clear()
            return colors
        now = time.monotonic()
        self._delayed.append((now, colors))
        ready = None
        while self._delayed and now - self._delayed[0][0] >= delay_s:
            ready = self._delayed.popleft()[1]
        return ready
