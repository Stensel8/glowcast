"""Colour maths, and the video engine that turns screen colours into light colours.

The engine works per light channel: the trimean colours of the channel's
screen zones are averaged as light (linear RGB), cleaned up (near-black goes
dark, near-grey goes neutral white), and smoothed in OKLab with a time
constant that slows down in dark scenes so they don't flicker. Hue Sync's
engine works along the same lines (its settings are named after trimean
sampling, a darkness threshold, desaturation and dark smoothing); the maths
and tuning here are our own. OKLab formulas: Björn Ottosson, public domain.
"""

import numpy as np

from glowcast.presets import VideoPreset

D65_XY = (0.3127, 0.3290)
WARM_WHITE_XY = (0.4578, 0.4101)  # about 2700 K, used for background lighting

DARK_L = 0.15  # OKLab lightness below which a channel counts as black (~sRGB 10)
BRIGHT_L = 0.6  # lightness from which the normal (fast) response applies
DESATURATION_FLOOR = 0.015  # chroma below this is shown as neutral white
DESATURATION_CEILING = 0.05  # full colour from this chroma on
MAX_CHROMA = 0.33

Color = tuple[tuple[float, float], int]  # ((x, y), brightness 0-254)

_RGB_TO_XYZ = np.array(
    [
        [0.4124564, 0.3575761, 0.1804375],
        [0.2126729, 0.7151522, 0.0721750],
        [0.0193339, 0.1191920, 0.9503041],
    ]
)
_XYZ_TO_RGB = np.linalg.inv(_RGB_TO_XYZ)
_XYZ_TO_RGB_ROWS = _XYZ_TO_RGB.tolist()
_RGB_TO_LMS = np.array(
    [
        [0.4122214708, 0.5363325363, 0.0514459929],
        [0.2119034982, 0.6806995451, 0.1073969566],
        [0.0883024619, 0.2817188376, 0.6299787005],
    ]
)
_LMS_TO_LAB = np.array(
    [
        [0.2104542553, 0.7936177850, -0.0040720468],
        [1.9779984951, -2.4285922050, 0.4505937099],
        [0.0259040371, 0.7827717662, -0.8086757660],
    ]
)
_LAB_TO_LMS = np.linalg.inv(_LMS_TO_LAB)
_LMS_TO_RGB = np.linalg.inv(_RGB_TO_LMS)


# --- arrays (rows of RGB / Lab) -----------------------------------------------


def srgb_to_linear(srgb: np.ndarray) -> np.ndarray:
    """sRGB in 0-1 to linear light."""
    return np.where(srgb <= 0.04045, srgb / 12.92, ((srgb + 0.055) / 1.055) ** 2.4)


def linear_to_srgb(linear: np.ndarray) -> np.ndarray:
    linear = np.clip(linear, 0.0, 1.0)
    return np.where(linear <= 0.0031308, linear * 12.92, 1.055 * linear ** (1 / 2.4) - 0.055)


def linear_to_oklab(rgb: np.ndarray) -> np.ndarray:
    return np.cbrt(np.clip(rgb, 0.0, None) @ _RGB_TO_LMS.T) @ _LMS_TO_LAB.T


def oklab_to_linear(lab: np.ndarray) -> np.ndarray:
    return ((lab @ _LAB_TO_LMS.T) ** 3) @ _LMS_TO_RGB.T


def linear_to_xy(rgb: np.ndarray) -> np.ndarray:
    xyz = np.clip(rgb, 0.0, None) @ _RGB_TO_XYZ.T
    total = xyz.sum(axis=-1, keepdims=True)
    xy = xyz[..., :2] / np.where(total > 0, total, 1.0)
    return np.where(total > 0, xy, D65_XY)


# --- single colours (scenes, palettes, swatches) ------------------------------


def hex_to_rgb(hex_color: str) -> tuple[float, float, float]:
    return tuple(int(hex_color[i : i + 2], 16) / 255.0 for i in (1, 3, 5))


def rgb_to_hex(rgb) -> str:
    return "#" + "".join(f"{round(min(1.0, max(0.0, c)) * 255):02X}" for c in rgb)


def hex_to_xy(hex_color: str) -> tuple[float, float]:
    x, y = linear_to_xy(srgb_to_linear(np.array(hex_to_rgb(hex_color))))
    return float(x), float(y)


def hex_to_oklab(hex_color: str) -> tuple[float, float, float]:
    return tuple(float(v) for v in linear_to_oklab(srgb_to_linear(np.array(hex_to_rgb(hex_color)))))


def oklab_to_xy(lab) -> tuple[float, float]:
    x, y = linear_to_xy(oklab_to_linear(np.array(lab)))
    return float(x), float(y)


def xy_to_hex(x: float, y: float) -> str:
    """Brightest sRGB colour with this chromaticity (for swatches)."""
    if y <= 0:
        return "#FFFFFF"
    rgb = np.clip(np.array([x / y, 1.0, (1 - x - y) / y]) @ _XYZ_TO_RGB.T, 0.0, None)
    peak = rgb.max()
    return rgb_to_hex(linear_to_srgb(rgb / peak) if peak > 0 else rgb)


def xy_to_rgb(x: float, y: float) -> tuple[float, float, float]:
    """Brightest sRGB colour (0-1) with this chromaticity; pure Python, for per-frame use."""
    if y <= 0:
        return 1.0, 1.0, 1.0
    big_x, big_z = x / y, (1 - x - y) / y
    linear = [max(0.0, row[0] * big_x + row[1] + row[2] * big_z) for row in _XYZ_TO_RGB_ROWS]
    peak = max(linear) or 1.0
    return tuple(_encode(c / peak) for c in linear)


def _encode(linear: float) -> float:
    """Linear light to sRGB (0-1)."""
    return linear * 12.92 if linear <= 0.0031308 else 1.055 * linear ** (1 / 2.4) - 0.055


def mirek_to_xy(mirek: int) -> tuple[float, float]:
    """Point on the black-body curve (Kim et al. approximation)."""
    t = 1e6 / max(40, min(600, mirek))
    t = max(1667.0, min(25000.0, t))
    if t <= 4000:
        x = -0.2661239e9 / t**3 - 0.2343589e6 / t**2 + 0.8776956e3 / t + 0.179910
    else:
        x = -3.0258469e9 / t**3 + 2.1070379e6 / t**2 + 0.2226347e3 / t + 0.240390
    if t <= 2222:
        y = -1.1063814 * x**3 - 1.34811020 * x**2 + 2.18555832 * x - 0.20219683
    elif t <= 4000:
        y = -0.9549476 * x**3 - 1.37418593 * x**2 + 2.09137015 * x - 0.16748867
    else:
        y = 3.0817580 * x**3 - 5.87338670 * x**2 + 3.75112997 * x - 0.37001483
    return x, y


# --- video engine ---------------------------------------------------------------


class VideoEngine:
    """Smooths per-channel screen colours into light colours (keeps state)."""

    def __init__(self):
        self._channels: list[int] = []
        self._lab: np.ndarray | None = None

    def reset(self) -> None:
        self._lab = None

    def process(
        self,
        channel_linear: dict[int, np.ndarray],
        preset: VideoPreset,
        dt: float,
        brightness_scale: float = 1.0,
    ) -> dict[int, Color]:
        """Linear RGB per channel -> ((x, y), brightness) per channel.

        dt is the time since the previous call; colours glide towards their
        target in between, so lights can update faster than the screen does.
        """
        channels = sorted(channel_linear)
        if not channels:
            return {}
        target = linear_to_oklab(np.array([channel_linear[c] for c in channels], dtype=float))

        chroma = np.hypot(target[:, 1], target[:, 2])
        keep = np.clip((chroma - DESATURATION_FLOOR) / (DESATURATION_CEILING - DESATURATION_FLOOR), 0.0, 1.0)
        gain = np.minimum(keep * preset.saturation, MAX_CHROMA / np.maximum(chroma, 1e-9))
        target[:, 1:] *= gain[:, None]

        if self._lab is None or channels != self._channels:
            self._channels, self._lab = channels, target.copy()
        previous = self._lab

        # OKLab scales with light: shrinking L, a and b together (the same
        # alpha below) dims a colour without changing its hue.
        target[target[:, 0] < DARK_L] = 0.0

        level = np.clip(np.maximum(target[:, 0], previous[:, 0]) / BRIGHT_L, 0.0, 1.0)
        tau = preset.dark_response_s + (preset.response_s - preset.dark_response_s) * level
        alpha = 1.0 - np.exp(-max(dt, 0.0) / np.maximum(tau, 1e-3))
        self._lab = previous + (target - previous) * alpha[:, None]

        linear = np.clip(oklab_to_linear(self._lab), 0.0, None)
        xy = linear_to_xy(linear)
        brightness = linear_to_srgb(linear).max(axis=1) * 254.0 * brightness_scale
        return {
            channel: ((float(xy[i, 0]), float(xy[i, 1])), int(min(254, round(brightness[i]))))
            for i, channel in enumerate(channels)
        }


def with_background_light(color: Color, floor: int) -> Color:
    """Never go darker than floor; dark colours turn into warm white."""
    (x, y), brightness = color
    if brightness >= floor or floor <= 0:
        return color
    t = 1.0 - brightness / floor
    return (x + (WARM_WHITE_XY[0] - x) * t, y + (WARM_WHITE_XY[1] - y) * t), floor
