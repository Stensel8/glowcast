"""Music mode: drive the entertainment zone from desktop audio.

Pipeline: AudioCapture (spectrum + per-channel loudness, 50 fps)
-> AudioFeatureExtractor (band levels relative to recent loudness, stereo
pan, small and big hits, silence) -> MusicEffect (per-light colour and
brightness) -> EntertainmentStream.

The split into bass-driven brightness, small hits that pulse and big hits
that shuffle palette colours between light groups follows how Hue Sync's
music mode behaves; the analysis and all tuning here are our own.
"""

import contextlib
import math
import queue
import random
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from functools import lru_cache
from statistics import fmean, pstdev
from typing import TYPE_CHECKING, Optional

from glowcast.audio import BAND_WIDTH_HZ, AudioCapture, AudioFrame
from glowcast.colors import hex_to_oklab, oklab_to_xy
from glowcast.presets import (
    AUDIO_EFFECTS,
    DEFAULT_INTENSITY,
    DEFAULT_PALETTE,
    MUSIC_PALETTES,
    MUSIC_PRESETS,
    PALETTE_SIZE,
    MusicPreset,
)

if TYPE_CHECKING:
    from glowcast.config.settings_manager import MusicSettings
    from glowcast.entertainment import EntertainmentStream
    from glowcast.hue_bridge import HueBridge

BAND_RANGES_HZ = {"bass": (30.0, 150.0), "mid": (150.0, 2000.0), "high": (2000.0, 8000.0)}
SILENCE_DB = -75.0  # mean band level below which the audio counts as quiet
SILENCE_HOLD_S = 2.0  # quiet must last this long before the lights settle
PEAK_DECAY_DB_PER_S = 3.0  # how quickly the loudness reference forgets old peaks
ONSET_HISTORY = 75  # ~1.5 s of onset values for the adaptive hit thresholds
MINOR_MIN_DB = 2.5  # smallest rise (dB) that can count as a small hit
MAJOR_MIN_DB = 6.0  # smallest bass rise (dB) that can count as a big hit
MINOR_MIN_INTERVAL_S = 0.12
AMBIENT_DRIFT_FRAMES = 300  # in silence, rotate colours every ~6 s

Color = tuple[tuple[float, float], int]  # ((x, y), brightness 0-254)


@dataclass
class AudioFeatures:
    bass: float  # 0-1, relative to the recent loudness of that band
    mid: float
    high: float
    pan: float  # -1 = all left, +1 = all right
    minor_event: float  # strength (0-1) of a small hit this frame, else 0
    major_event: float  # strength (0-1) of a big hit this frame, else 0
    silent: bool


class AudioFeatureExtractor:
    def __init__(self, band_width_hz: float = BAND_WIDTH_HZ):
        self._slices = {
            name: slice(max(1, int(lo / band_width_hz)), int(hi / band_width_hz) + 1)
            for name, (lo, hi) in BAND_RANGES_HZ.items()
        }
        self._peak_db = dict.fromkeys(BAND_RANGES_HZ, SILENCE_DB)
        self._slow_db: dict[str, float | None] = {"bass": None, "mid": None}
        self._onsets = {name: deque(maxlen=ONSET_HISTORY) for name in self._slow_db}
        self._last_minor = -math.inf
        self._last_major = -math.inf
        self._quiet_since: float | None = None
        self._last_time: float | None = None

    def process(self, frame: AudioFrame, now: float, preset: MusicPreset) -> AudioFeatures:
        dt = 0.0 if self._last_time is None else min(0.1, now - self._last_time)
        self._last_time = now

        powers = [10.0 ** (db / 10.0) for db in frame.magnitudes_db]
        band_db = {name: 10.0 * math.log10(fmean(powers[sl]) + 1e-12) for name, sl in self._slices.items()}
        overall_db = 10.0 * math.log10(fmean(powers[1:]) + 1e-12)

        if overall_db < SILENCE_DB:
            self._quiet_since = self._quiet_since if self._quiet_since is not None else now
        else:
            self._quiet_since = None
        quiet = self._quiet_since is not None
        silent = quiet and now - self._quiet_since >= SILENCE_HOLD_S

        levels = {}
        for name, db in band_db.items():
            # The reference follows peaks instantly and forgets them slowly,
            # and never drops so low that background noise fills the range.
            peak = max(
                db,
                self._peak_db[name] - PEAK_DECAY_DB_PER_S * dt,
                SILENCE_DB + preset.dynamic_range_db,
            )
            self._peak_db[name] = peak
            floor = peak - preset.dynamic_range_db
            levels[name] = min(1.0, max(0.0, (db - floor) / preset.dynamic_range_db))

        minor, major = self._detect_hits(band_db, now, preset, quiet)
        return AudioFeatures(
            bass=levels["bass"],
            mid=levels["mid"],
            high=levels["high"],
            pan=self._pan(frame.channel_rms_db),
            minor_event=minor,
            major_event=major,
            silent=silent,
        )

    def _onset(self, name: str, db: float) -> tuple[float, float, float]:
        """Rise of a band above its own short-term average, plus that rise's history stats."""
        slow = self._slow_db[name]
        if slow is None:
            slow = db
        rise = max(0.0, db - slow)
        self._slow_db[name] = slow + (db - slow) * 0.15  # ~130 ms average
        history = self._onsets[name]
        mean = fmean(history) if history else 0.0
        std = pstdev(history) if len(history) > 1 else 0.0
        history.append(rise)
        return rise, mean, std

    def _detect_hits(
        self, band_db: dict[str, float], now: float, preset: MusicPreset, quiet: bool
    ) -> tuple[float, float]:
        bass_rise, bass_mean, bass_std = self._onset("bass", band_db["bass"])
        mid_rise, mid_mean, mid_std = self._onset("mid", band_db["mid"])
        if quiet:
            return 0.0, 0.0

        major = 0.0
        major_threshold = max(MAJOR_MIN_DB, bass_mean + 2.5 * bass_std)
        if bass_rise > major_threshold and now - self._last_major >= preset.shuffle_delay_s:
            major = min(1.0, (bass_rise - major_threshold) / (bass_std + 1.0) + 0.5)
            self._last_major = now

        minor = 0.0
        for rise, mean, std in ((bass_rise, bass_mean, bass_std), (mid_rise, mid_mean, mid_std)):
            threshold = max(MINOR_MIN_DB, mean + 1.2 * std)
            if rise > threshold:
                minor = max(minor, min(1.0, (rise - threshold) / (std + 1.0) + 0.3))
        if minor and now - self._last_minor >= MINOR_MIN_INTERVAL_S:
            self._last_minor = now
        else:
            minor = 0.0
        return minor, major

    @staticmethod
    def _pan(channel_rms_db: tuple[float, float]) -> float:
        left, right = (10.0 ** (db / 20.0) for db in channel_rms_db)
        total = left + right
        return (right - left) / total if total > 1e-6 else 0.0


def palette_colors(settings: "MusicSettings") -> tuple[str, ...]:
    """Hex colours of the chosen palette, built in or the user's own."""
    if settings.palette in MUSIC_PALETTES:
        return MUSIC_PALETTES[settings.palette].colors
    for palette in settings.custom_palettes:
        if palette["id"] == settings.palette:
            return tuple(palette["colors"])
    return MUSIC_PALETTES[DEFAULT_PALETTE].colors


@lru_cache(maxsize=32)
def _palette_lab(colors: tuple[str, ...]) -> tuple:
    return tuple(hex_to_oklab(c) for c in colors)


class MusicEffect:
    """Turns audio features into a colour and brightness per channel.

    Lights are split into colour groups from left to right (one group per
    light by default). Each group shows one palette colour; big hits rotate
    or shuffle the colours between groups. Low lights follow the bass, high
    lights the mids and highs, and with localisation the stereo balance
    shifts energy between left and right lights.
    """

    def __init__(self, channel_positions: dict[int, dict], rng: random.Random | None = None):
        self._positions = {
            cid: (float(p.get("x", 0)), float(p.get("y", 0)), float(p.get("z", 0)))
            for cid, p in channel_positions.items()
        }
        self._channels = sorted(self._positions)
        self._rng = rng or random.Random()
        self._envelope = dict.fromkeys(self._channels, 0.0)
        self._color: dict[int, tuple[float, float, float]] = {}
        self._pulse = 0.0
        self._group_setting: int | None = None
        self._group_of: dict[int, int] = {}
        self._group_color: list[int] = []
        self._silent_frames = 0

    def _regroup(self, groups: int) -> None:
        count = len(self._channels)
        n = count if groups <= 0 else max(1, min(groups, count))
        order = sorted(self._channels, key=lambda c: (self._positions[c][0], self._positions[c][2], c))
        self._group_of = {cid: i * n // count for i, cid in enumerate(order)}
        self._group_color = [g % PALETTE_SIZE for g in range(n)]
        self._group_setting = groups

    def _shuffle(self, randomly: bool) -> None:
        if randomly and len(self._group_color) > 1:
            previous = list(self._group_color)
            while self._group_color == previous:
                self._group_color = [self._rng.randrange(PALETTE_SIZE) for _ in previous]
        else:
            self._group_color = [(c + 1) % PALETTE_SIZE for c in self._group_color]

    def update(
        self,
        features: AudioFeatures,
        palette_colors: tuple[str, ...],
        preset: MusicPreset,
        max_brightness: int,
        groups: int,
        localization: bool,
    ) -> dict[int, Color]:
        if groups != self._group_setting:
            self._regroup(groups)
        palette = _palette_lab(palette_colors)

        if features.silent:
            self._silent_frames += 1
            if self._silent_frames % AMBIENT_DRIFT_FRAMES == 0:
                self._shuffle(randomly=False)
        else:
            self._silent_frames = 0
            if features.major_event:
                self._shuffle(preset.shuffle_random)

        hit = max(features.minor_event, features.major_event * 1.3)
        self._pulse = max(self._pulse * preset.pulse_decay, hit * preset.pulse)

        colors: dict[int, Color] = {}
        for cid in self._channels:
            x, _y, z = self._positions[cid]
            if features.silent:
                target = preset.silence_level
            else:
                height = min(1.0, max(0.0, (z + 1.0) / 2.0))  # 0 floor .. 1 ceiling
                level = (1.0 - height) * features.bass
                level += height * (0.6 * features.mid + 0.4 * features.high)
                level += preset.bass_mod * height * features.bass
                if localization:
                    level *= min(1.7, max(0.3, 1.0 + 0.8 * features.pan * max(-1.0, min(1.0, x))))
                drive = min(1.0, max(0.0, level + self._pulse))
                target = preset.floor + (1.0 - preset.floor) * drive

            envelope = self._envelope[cid]
            step = preset.attack if target > envelope else preset.release
            envelope += (target - envelope) * step
            self._envelope[cid] = envelope

            goal = palette[self._group_color[self._group_of[cid]] % len(palette)]
            current = self._color.get(cid, goal)
            current = tuple(c + (g - c) * preset.color_speed for c, g in zip(current, goal))
            self._color[cid] = current

            colors[cid] = (oklab_to_xy(current), round(max_brightness * envelope))
        return colors


class MusicController:
    """Runs the audio -> lights loop on a worker thread (mirrors SyncController)."""

    def __init__(
        self,
        bridge: "HueBridge",
        settings: "MusicSettings",
        entertainment_stream: Optional["EntertainmentStream"] = None,
        brightness: Callable[[], float] = lambda: 1.0,
    ):
        self.bridge = bridge
        self.settings = settings
        self.entertainment_stream = entertainment_stream
        self.brightness = brightness  # the global brightness, 0-1, read every frame
        self.running = False
        self.thread: threading.Thread | None = None
        self.queue: queue.Queue = queue.Queue(maxsize=100)
        self.last_error = ""
        self._capture: AudioCapture | None = None
        self._lock = threading.Lock()
        self._stats = {"fps": 0.0}
        # Newest analysis result, replaced every frame (a plain reference
        # swap, so the GUI can read it from its frame clock without locking)
        self.latest_features: AudioFeatures | None = None

    def start(self) -> bool:
        """Open desktop audio and start the loop; on failure last_error says why."""
        if self.running:
            return True
        self.last_error = ""
        capture = AudioCapture(self.settings.audio_device, self.settings.vocal_filter)
        if not capture.start():
            self.last_error = f"Could not open desktop audio: {capture.last_error}"
            return False
        self._capture = capture
        self.running = True
        self.thread = threading.Thread(target=self._loop, daemon=True, name="MusicLoop")
        self.thread.start()
        return True

    def stop(self) -> None:
        if not self.running:
            return
        self.running = False
        if self.thread:
            self.thread.join(timeout=3)
        if self._capture:
            self._capture.stop()
            self._capture = None

    def is_running(self) -> bool:
        return self.running

    def get_status(self) -> tuple | None:
        try:
            return self.queue.get_nowait()
        except queue.Empty:
            return None

    def get_stats(self) -> dict:
        with self._lock:
            return dict(self._stats)

    def _queue_status(self, status_type: str, message, data=None) -> None:
        with contextlib.suppress(queue.Full):
            self.queue.put_nowait((status_type, message, data))

    def _loop(self) -> None:
        extractor = AudioFeatureExtractor()
        effect: MusicEffect | None = None
        effect_stream = None
        frame_times: deque = deque(maxlen=50)

        while self.running:
            frame = self._capture.read(timeout_s=0.5)
            if frame is None:
                if self._capture.last_error:
                    self._queue_status("error", f"Audio capture stopped: {self._capture.last_error}")
                    break
                continue

            now = time.monotonic()
            preset = MUSIC_PRESETS.get(self.settings.intensity, MUSIC_PRESETS[DEFAULT_INTENSITY])
            features = extractor.process(frame, now, preset)
            self.latest_features = features

            stream = self.entertainment_stream
            if stream is None:
                continue
            if not stream.is_connected():
                if not stream.reconnect_if_dropped(self.bridge):
                    continue
                effect = None
            if effect is None or effect_stream is not stream:
                effect = MusicEffect(stream.get_channel_positions())
                effect_stream = stream

            colors = effect.update(
                features,
                palette_colors(self.settings),
                preset,
                round(254 * self.brightness()),
                self.settings.color_groups,
                self.settings.localization,
            )
            stream.send_colors_xy(colors)

            frame_times.append(now)
            if len(frame_times) > 1:
                with self._lock:
                    self._stats["fps"] = (len(frame_times) - 1) / (frame_times[-1] - frame_times[0])

        self.latest_features = None
        self._queue_status("status", "stopped")


def spectral_flux(previous: list[float] | None, magnitudes_db: list[float]) -> float:
    """How much the spectrum grew since the last frame (onset strength).

    Sums the rise of log-compressed band magnitudes, a standard onset
    measure: a gunshot or drum hit scores high, steady sound scores low.
    """
    if previous is None:
        return 0.0
    return sum(max(0.0, now - before) for now, before in zip(magnitudes_db, previous)) / 20.0


class AudioListener:
    """Desktop audio analysed in the background, for "Use audio for effect".

    Like Hue Sync's, sound bursts (shots, hits, explosions) boost the lights'
    brightness by a multiplier, measured as spectral flux against its running
    average and a slowly falling peak; between bursts the screen's own
    brightness stays. Left and right sound drive left and right lights, and
    in silence the effect steps aside. The tuning is our own.
    """

    ATTACK_S = 0.03
    AVERAGE_S = 2.0  # running average of the flux: the burst threshold
    PEAK_FALL_S = 4.0  # how slowly the loudest recent burst is forgotten
    WARM_UP_S = 1.5  # time to learn the levels after starting or after silence

    def __init__(self, device: str):
        self._capture = AudioCapture(device)
        self._running = False
        self.intensity = DEFAULT_INTENSITY
        self.last_error = ""
        self._previous_db: list[float] | None = None
        self._average = 0.0
        self._peak = 0.0
        self._burst = 0.0  # 0-1, the current burst envelope
        self._pan = 0.0
        self._active = 0.0  # 0 = effect off (silence, warming up), 1 = fully on

    def start(self) -> bool:
        if not self._capture.start():
            self.last_error = self._capture.last_error
            return False
        self._running = True
        threading.Thread(target=self._loop, daemon=True, name="AudioListener").start()
        return True

    def stop(self) -> None:
        self._running = False

    def gain(self, x: float = 0.0, stereo: bool = True) -> float:
        """Brightness factor for a light at x (-1 left .. 1 right)."""
        effect = AUDIO_EFFECTS.get(self.intensity, AUDIO_EFFECTS[DEFAULT_INTENSITY])
        burst = self._burst
        if stereo:
            burst = min(1.0, burst * min(1.6, max(0.3, 1.0 + 0.9 * self._pan * x)))
        factor = effect.rest + (effect.peak - effect.rest) * burst
        return 1.0 + self._active * (factor - 1.0)

    def step(self, flux: float, pan: float, silent: bool, dt: float) -> None:
        """Advance the effect by one analysis frame."""
        effect = AUDIO_EFFECTS.get(self.intensity, AUDIO_EFFECTS[DEFAULT_INTENSITY])
        self._active += ((0.0 if silent else 1.0) - self._active) * (1 - math.exp(-dt / self.WARM_UP_S))
        if self._average == 0.0:
            self._average = self._peak = flux  # first sound: start from its level
        self._average += (flux - self._average) * (1 - math.exp(-dt / self.AVERAGE_S))
        floor = self._average * 2.0  # what the peak falls back to
        self._peak = max(flux, floor + (self._peak - floor) * math.exp(-dt / self.PEAK_FALL_S))

        threshold = self._average * effect.sensitivity
        strength = (flux - threshold) / max(self._peak - threshold, 1e-6) if flux > threshold else 0.0
        target = min(1.0, strength)
        tau = self.ATTACK_S if target > self._burst else effect.release_s
        self._burst += (target - self._burst) * (1 - math.exp(-dt / tau))
        self._pan += (pan - self._pan) * (1 - math.exp(-dt / 0.1))

    def _loop(self) -> None:
        extractor = AudioFeatureExtractor()
        last = time.monotonic()
        while self._running:
            frame = self._capture.read(timeout_s=0.5)
            if frame is None:
                if self._capture.last_error:
                    break
                continue
            now = time.monotonic()
            features = extractor.process(frame, now, MUSIC_PRESETS[DEFAULT_INTENSITY])
            flux = spectral_flux(self._previous_db, frame.magnitudes_db)
            self._previous_db = frame.magnitudes_db
            self.step(flux, features.pan, features.silent, min(0.1, now - last))
            last = now
        self._capture.stop()
        self._burst, self._active = 0.0, 0.0
