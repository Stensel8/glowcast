"""Letterbox and pillarbox bars: ignored automatically, as in Hue Sync."""

import numpy as np

BLACK_LEVEL = 10  # luminance (0-255) up to which a row or column counts as black
DETECT_EVERY = 30  # frames between detections (about once a second)
SMOOTHING = 0.3  # share of the way to a newly detected crop taken per frame
MIN_CONTENT = 0.5  # never crop away more than half of the width or height
MIN_CROP = 0.02  # bars thinner than 2 % of the image are left alone
LUMA = np.array([0.299, 0.587, 0.114], dtype=np.float32)


def _black_run(profile: np.ndarray) -> int:
    """How many entries at the start of a row/column brightness profile are black."""
    bright = np.flatnonzero(profile > BLACK_LEVEL)
    return int(bright[0]) if bright.size else len(profile)


class BlackBarDetector:
    def __init__(self):
        self._size: tuple[int, int] | None = None
        self._frame = 0
        self._bars = np.zeros(4)  # top, bottom, left, right in pixels (smoothed)
        self._target = np.zeros(4)

    def crop(self, image: np.ndarray) -> np.ndarray:
        """The image without its black bars (a view, no copy)."""
        height, width = image.shape[:2]
        if self._size != (height, width):
            self._size, self._frame = (height, width), 0
            self._bars, self._target = np.zeros(4), np.zeros(4)
        if self._frame % DETECT_EVERY == 0:
            self._target = self._detect(image)
        self._frame += 1
        self._bars += (self._target - self._bars) * SMOOTHING
        top, bottom, left, right = (int(v) for v in self._bars)
        if top + bottom < MIN_CROP * height and left + right < MIN_CROP * width:
            return image
        return image[top : height - bottom, left : width - right]

    @staticmethod
    def _detect(image: np.ndarray) -> np.ndarray:
        luminance = image[:, :, :3].astype(np.float32) @ LUMA
        rows, cols = luminance.mean(axis=1), luminance.mean(axis=0)
        top, bottom = _black_run(rows), _black_run(rows[::-1])
        left, right = _black_run(cols), _black_run(cols[::-1])
        height, width = luminance.shape
        if height - top - bottom < MIN_CONTENT * height:
            top = bottom = 0
        if width - left - right < MIN_CONTENT * width:
            left = right = 0
        return np.array([top, bottom, left, right], dtype=float)
