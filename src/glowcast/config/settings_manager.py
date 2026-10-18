"""Settings, stored as JSON in the (Flatpak) config directory.

Like Hue Sync, only choices a user actually makes are settings; tuning values
(frame rate, zone grid, capture scale, black-bar detection) are fixed in code.
"""

import json
import os
import re
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path

from glowcast.presets import (
    DEFAULT_INTENSITY,
    DEFAULT_PALETTE,
    DEFAULT_SCENE,
    INTENSITY_LABELS,
    MUSIC_PALETTES,
    SCENE_PRESETS,
)
from glowcast.utils.logging import timed_print

MODES = ("scenes", "games", "music", "video")
MAX_SYNC_DELAY_MS = 900
_HEX_COLOR = re.compile(r"^#[0-9A-Fa-f]{6}$")


@dataclass
class HueSettings:
    bridge_ip: str = ""
    app_key: str = ""
    client_key: str = ""  # PSK for DTLS entertainment streaming
    entertainment_config_id: str = ""  # selected entertainment area


@dataclass
class CaptureSettings:  # Hue Sync's display preferences
    restore_token: str = ""  # portal token: remembers which monitors to share
    capture_area: list = field(default_factory=lambda: [0.0, 0.0, 1.0, 1.0])  # x0 y0 x1 y1
    sync_delay_ms: int = 0  # for wireless displays that run behind


@dataclass
class LightSettings:
    brightness: float = 1.0  # 0.01-1: one brightness for every mode, like Hue Sync's single slider


@dataclass
class VideoSettings:  # video and games mode
    intensity: str = DEFAULT_INTENSITY
    use_audio: bool = False  # "Use audio for effect"
    background_light: bool = False  # minimum light level whatever the screen shows


@dataclass
class MusicSettings:
    palette: str = DEFAULT_PALETTE  # key into MUSIC_PALETTES, or a custom palette id
    intensity: str = DEFAULT_INTENSITY
    color_groups: int = 0  # light channels: 1-7, 0 = match number of lights
    localization: bool = True  # left/right audio drives left/right lights
    vocal_filter: bool = False  # ignore centred vocals
    audio_device: str = "@DEFAULT_MONITOR@"  # PulseAudio source; default = speaker output
    custom_palettes: list = field(default_factory=list)  # [{"id", "name", "colors"}]


@dataclass
class SceneSettings:
    scene: str = DEFAULT_SCENE  # preset key, custom scene id or "bridge:<id>"
    dynamic: bool = False  # colours slowly move between the lights
    custom: list = field(default_factory=list)  # [{"id", "name", "colors"}]


@dataclass
class UISettings:  # Hue Sync's general preferences
    start_at_startup: bool = False
    minimize_at_startup: bool = False  # start in the tray
    minimize_to_tray_on_sync: bool = False
    start_sync_at_launch: bool = False
    default_mode: str = "last"  # "last" or one of MODES
    last_mode: str = "video"
    lights_on_at_launch: bool = False
    launch_scene: str = DEFAULT_SCENE
    lights_off_on_close: bool = False
    notifications: bool = True
    close_quits: bool = False  # otherwise the close button hides to the tray
    shortcuts: bool = False  # keyboard shortcuts were set up


@dataclass
class Settings:
    hue: HueSettings = field(default_factory=HueSettings)
    lights: LightSettings = field(default_factory=LightSettings)
    capture: CaptureSettings = field(default_factory=CaptureSettings)
    sync: VideoSettings = field(default_factory=VideoSettings)  # video mode
    games: VideoSettings = field(default_factory=VideoSettings)
    music: MusicSettings = field(default_factory=MusicSettings)
    scenes: SceneSettings = field(default_factory=SceneSettings)
    ui: UISettings = field(default_factory=UISettings)


def _load(cls, data):
    """Build a settings dataclass from saved JSON, ignoring unknown keys.

    Keys from older versions (or removed settings) must not make the whole
    file fail to load and reset everything to defaults.
    """
    known = {f.name for f in fields(cls)}
    return cls(**{k: v for k, v in (data or {}).items() if k in known})


def _clamp(value, low, high, default):
    try:
        return max(low, min(high, type(default)(value)))
    except (TypeError, ValueError):
        return default


def valid_custom_entries(entries) -> list:
    """Custom palettes or scenes: keep well-formed ones only."""
    valid = []
    for entry in entries if isinstance(entries, list) else []:
        if not isinstance(entry, dict):
            continue
        colors = [c for c in entry.get("colors", []) if isinstance(c, str) and _HEX_COLOR.match(c)]
        if entry.get("id") and entry.get("name") and colors:
            valid.append({"id": str(entry["id"]), "name": str(entry["name"]), "colors": colors[:5]})
    return valid


class SettingsManager:
    _instance: "SettingsManager | None" = None

    def __new__(cls) -> "SettingsManager":
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._init()
        return cls._instance

    @classmethod
    def get_instance(cls) -> "SettingsManager":
        return cls()

    def _init(self):
        self._settings = Settings()
        self._config_dir = self._get_config_dir()
        self._settings_file = self._config_dir / "settings.json"
        self._load_settings()

    def _get_config_dir(self) -> Path:
        # In Flatpak XDG_CONFIG_HOME maps to ~/.var/app/<app-id>/config
        config_home = os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config"
        return Path(config_home) / "glowcast"

    def __getattr__(self, name):
        # settings.hue, settings.music, ...: the sections of Settings
        if name.startswith("_"):
            raise AttributeError(name)
        return getattr(self._settings, name)

    @property
    def settings(self) -> Settings:
        return self._settings

    def video_settings(self, mode: str) -> VideoSettings:
        return self._settings.games if mode == "games" else self._settings.sync

    def _load_settings(self):
        if self._settings_file.exists():
            try:
                data = json.loads(self._settings_file.read_text())
                for section in fields(Settings):
                    default = getattr(self._settings, section.name)
                    setattr(self._settings, section.name, _load(type(default), data.get(section.name)))
            except (json.JSONDecodeError, TypeError, AttributeError) as e:
                timed_print(f"Error loading settings: {e}")
        self._validate_settings()

    def save(self):
        self._config_dir.mkdir(parents=True, exist_ok=True)
        self._validate_settings()
        self._settings_file.write_text(json.dumps(asdict(self._settings), indent=2))

    def reset_preferences(self):
        """Back to defaults, keeping the bridge pairing and monitor choice.

        Values are reset in place: running components hold these objects.
        """
        defaults = Settings()
        for section in fields(Settings):
            if section.name == "hue":
                continue
            current, default = getattr(self._settings, section.name), getattr(defaults, section.name)
            for f in fields(current):
                if (section.name, f.name) != ("capture", "restore_token"):
                    setattr(current, f.name, getattr(default, f.name))
        self.save()

    def _validate_settings(self):
        s = self._settings
        s.lights.brightness = _clamp(s.lights.brightness, 0.01, 1.0, 1.0)
        for video in (s.sync, s.games):
            if video.intensity not in INTENSITY_LABELS:
                video.intensity = DEFAULT_INTENSITY
            video.use_audio = bool(video.use_audio)
            video.background_light = bool(video.background_light)

        capture = s.capture
        try:
            x0, y0, x1, y1 = (min(1.0, max(0.0, float(v))) for v in capture.capture_area)
            if x1 - x0 < 0.05 or y1 - y0 < 0.05:
                raise ValueError
            capture.capture_area = [x0, y0, x1, y1]
        except (TypeError, ValueError):
            capture.capture_area = [0.0, 0.0, 1.0, 1.0]
        capture.sync_delay_ms = _clamp(capture.sync_delay_ms, 0, MAX_SYNC_DELAY_MS, 0)

        music = s.music
        music.custom_palettes = valid_custom_entries(music.custom_palettes)
        custom_ids = {p["id"] for p in music.custom_palettes}
        if music.palette not in MUSIC_PALETTES and music.palette not in custom_ids:
            music.palette = DEFAULT_PALETTE
        if music.intensity not in INTENSITY_LABELS:
            music.intensity = DEFAULT_INTENSITY
        music.color_groups = _clamp(music.color_groups, 0, 7, 0)
        if not isinstance(music.audio_device, str) or not music.audio_device:
            music.audio_device = MusicSettings.audio_device

        scenes = s.scenes
        scenes.custom = valid_custom_entries(scenes.custom)
        known_scenes = set(SCENE_PRESETS) | {c["id"] for c in scenes.custom}
        if scenes.scene not in known_scenes and not str(scenes.scene).startswith("bridge:"):
            scenes.scene = DEFAULT_SCENE

        ui = s.ui
        if ui.default_mode not in (*MODES, "last"):
            ui.default_mode = "last"
        if ui.last_mode not in MODES:
            ui.last_mode = "video"
        if ui.launch_scene not in known_scenes and not str(ui.launch_scene).startswith("bridge:"):
            ui.launch_scene = DEFAULT_SCENE
