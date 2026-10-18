"""Intensities, music palettes and scenes.

Names follow Hue Sync (Subtle / Moderate / High / Extreme, its scene
gallery); every value is Glowcast's own tuning. Kept free of GStreamer/GTK
imports so settings validation can use the keys.
"""

from dataclasses import dataclass

INTENSITY_LABELS = {
    "subtle": "Subtle",
    "moderate": "Moderate",
    "high": "High",
    "extreme": "Extreme",
}
DEFAULT_INTENSITY = "moderate"


@dataclass(frozen=True)
class VideoPreset:
    response_s: float  # how quickly lights follow a bright screen (time constant)
    dark_response_s: float  # the same in dark scenes: slower, so they don't flicker
    saturation: float  # colour gain


# Video is smooth and cinematic, games react faster and punchier.
VIDEO_PRESETS = {
    "subtle": VideoPreset(0.30, 0.90, 1.00),
    "moderate": VideoPreset(0.12, 0.45, 1.10),
    "high": VideoPreset(0.06, 0.25, 1.20),
    "extreme": VideoPreset(0.03, 0.12, 1.30),
}
GAME_PRESETS = {
    "subtle": VideoPreset(0.15, 0.45, 1.05),
    "moderate": VideoPreset(0.06, 0.20, 1.15),
    "high": VideoPreset(0.03, 0.10, 1.25),
    "extreme": VideoPreset(0.01, 0.04, 1.35),
}


@dataclass(frozen=True)
class AudioEffectPreset:
    """ "Use audio for effect" in video and games mode."""

    rest: float  # brightness factor between sound bursts (1 = the screen's own)
    peak: float  # brightness factor at the strongest burst
    release_s: float  # how quickly a burst fades
    sensitivity: float  # a burst must exceed this many times the average sound change


AUDIO_EFFECTS = {
    "subtle": AudioEffectPreset(rest=1.0, peak=1.6, release_s=0.35, sensitivity=1.4),
    "moderate": AudioEffectPreset(rest=1.0, peak=2.5, release_s=0.25, sensitivity=1.2),
    "high": AudioEffectPreset(rest=0.95, peak=3.5, release_s=0.18, sensitivity=1.1),
    "extreme": AudioEffectPreset(rest=0.9, peak=5.0, release_s=0.12, sensitivity=1.0),
}


@dataclass(frozen=True)
class MusicPreset:
    floor: float  # lowest brightness (0-1) while music plays
    dynamic_range_db: float  # how far below the recent peak still lights up
    attack: float  # per-frame step towards a brighter target (at 50 fps)
    release: float  # per-frame step towards a dimmer target
    pulse: float  # extra brightness on a beat
    pulse_decay: float  # per-frame multiplier for that extra brightness
    bass_mod: float  # how much the bass also moves the upper lights
    color_speed: float  # per-frame step towards a new colour
    shuffle_delay_s: float  # minimum time between colour shuffles on big hits
    shuffle_random: bool  # shuffle colours randomly instead of rotating them
    silence_level: float  # brightness (0-1) the lights settle at in silence


MUSIC_PRESETS = {
    "subtle": MusicPreset(
        floor=0.35,
        dynamic_range_db=30.0,
        attack=0.20,
        release=0.03,
        pulse=0.10,
        pulse_decay=0.90,
        bass_mod=0.10,
        color_speed=0.02,
        shuffle_delay_s=4.0,
        shuffle_random=False,
        silence_level=0.30,
    ),
    "moderate": MusicPreset(
        floor=0.20,
        dynamic_range_db=26.0,
        attack=0.35,
        release=0.06,
        pulse=0.25,
        pulse_decay=0.85,
        bass_mod=0.20,
        color_speed=0.05,
        shuffle_delay_s=2.0,
        shuffle_random=False,
        silence_level=0.20,
    ),
    "high": MusicPreset(
        floor=0.10,
        dynamic_range_db=22.0,
        attack=0.55,
        release=0.10,
        pulse=0.40,
        pulse_decay=0.80,
        bass_mod=0.30,
        color_speed=0.10,
        shuffle_delay_s=1.0,
        shuffle_random=True,
        silence_level=0.15,
    ),
    "extreme": MusicPreset(
        floor=0.03,
        dynamic_range_db=18.0,
        attack=0.80,
        release=0.18,
        pulse=0.60,
        pulse_decay=0.75,
        bass_mod=0.40,
        color_speed=0.20,
        shuffle_delay_s=0.4,
        shuffle_random=True,
        silence_level=0.10,
    ),
}


@dataclass(frozen=True)
class Palette:
    name: str
    colors: tuple[str, ...]  # five sRGB hex colours


PALETTE_SIZE = 5

MUSIC_PALETTES = {
    "ember": Palette("Ember", ("#FF3B1F", "#FF7A00", "#FFB000", "#E0115F", "#FF4F81")),
    "neon": Palette("Neon", ("#FF00A8", "#8F00FF", "#00B3FF", "#00FFC6", "#B6FF00")),
    "citrus": Palette("Citrus", ("#FFE600", "#A8E000", "#FF9F1C", "#FFD166", "#4CD137")),
    "lagoon": Palette("Lagoon", ("#00C2C7", "#0077B6", "#48CAE4", "#2EC4B6", "#3A0CA3")),
    "polar_lights": Palette("Polar Lights", ("#00F5A0", "#00D9F5", "#7B2FF7", "#2D6BFF", "#9CFF6E")),
    "dusk": Palette("Dusk", ("#FF6B6B", "#FFA36C", "#C06C84", "#6C5B7B", "#F8B195")),
    "spectrum": Palette("Spectrum", ("#FF0000", "#FFA500", "#FFFF00", "#00FF00", "#0080FF")),
    "deep_night": Palette("Deep Night", ("#1B1F7A", "#4B2991", "#7A4DE0", "#1E5AA8", "#0B6E79")),
}
DEFAULT_PALETTE = "ember"


@dataclass(frozen=True)
class Scene:
    name: str
    colors: tuple[str, ...]  # spread over the lights from left to right
    brightness: int = 254  # 1-254, at a brightness setting of 100 %
    mirek: int = 0  # white scenes set a colour temperature instead


# Hue Sync's scene gallery. The four white scenes use the well-known Hue
# colour temperatures; the colours of the others are our own.
SCENE_PRESETS = {
    "relax": Scene("Relax", ("#FFAE5C",), 144, mirek=447),
    "read": Scene("Read", ("#FFCF9E",), 254, mirek=346),
    "concentrate": Scene("Concentrate", ("#FFEEDD",), 254, mirek=233),
    "energize": Scene("Energize", ("#E3ECFF",), 254, mirek=156),
    "blue_planet": Scene("Blue Planet", ("#0B3D91", "#1F7AE0", "#00B4D8", "#48CAE4", "#1A237E"), 200),
    "vapour_wave": Scene("Vapour Wave", ("#FF71CE", "#B967FF", "#01CDFE", "#05FFA1", "#FF4FA3"), 200),
    "tyrell": Scene("Tyrell", ("#FF8C00", "#FFB347", "#00A6A6", "#B34700", "#FFD27F"), 180),
    "disturbia": Scene("Disturbia", ("#5B0E91", "#C2185B", "#2E7D32", "#7B1FA2", "#B71C1C"), 170),
    "magneto": Scene("Magneto", ("#8E24AA", "#D81B60", "#3949AB", "#E040FB", "#5E35B1"), 190),
    "hal": Scene("Hal", ("#FF1A1A", "#B00000", "#FF4D00", "#800000", "#FF6666"), 160),
}
DEFAULT_SCENE = "read"
