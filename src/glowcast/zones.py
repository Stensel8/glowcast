"""Screen edge zones: the colour of each strip along the top, bottom, left and right."""

import numpy as np


def _trimean(blocks: np.ndarray) -> np.ndarray:
    """(zones, pixels, 3) -> (zones, 3): (Q1 + 2 * median + Q3) / 4 per colour.

    Unlike the mean, small bright details (a cursor, subtitles, a HUD) barely
    move it; unlike the median, it still blends the zone's colours.
    """
    q1, q2, q3 = np.percentile(blocks, (25, 50, 75), axis=1)
    return (q1 + 2 * q2 + q3) / 4


class ZoneProcessor:
    def __init__(self, rows: int = 16, cols: int = 16):
        self.rows = rows  # zones down the left and right edges
        self.cols = cols  # zones along the top and bottom edges

    def zone_ids(self) -> list[str]:
        return [
            *(f"{edge}_{i}" for edge in ("top", "bottom") for i in range(self.cols)),
            *(f"{edge}_{i}" for edge in ("left", "right") for i in range(self.rows)),
        ]

    def position(self, zone_id: str) -> tuple[float, float]:
        """Zone centre as (x, z): x from -1 (left) to 1 (right), z from -1 (bottom) to 1 (top)."""
        edge, index = zone_id.split("_")
        i = int(index)
        if edge in ("top", "bottom"):
            return -1.0 + (2 * i + 1) / self.cols, 1.0 if edge == "top" else -1.0
        return -1.0 if edge == "left" else 1.0, 1.0 - (2 * i + 1) / self.rows

    def process_image(self, image: np.ndarray) -> dict[str, np.ndarray]:
        """Trimean sRGB colour (0-255 floats) of every edge zone."""
        if image is None or image.ndim != 3 or image.shape[0] < 4 or image.shape[1] < 4:
            return {}
        image = image[:, :, :3]
        height, width = image.shape[:2]
        depth = max(2, min(width // self.cols, height // 8))
        zone_w, zone_h = width // self.cols, height // self.rows
        if zone_w < 1 or zone_h < 1:
            return {}

        def horizontal(strip: np.ndarray) -> np.ndarray:
            strip = strip[:, : zone_w * self.cols].reshape(depth, self.cols, zone_w, 3)
            return _trimean(strip.transpose(1, 0, 2, 3).reshape(self.cols, -1, 3))

        def vertical(strip: np.ndarray) -> np.ndarray:
            return _trimean(strip[: zone_h * self.rows].reshape(self.rows, -1, 3))

        colors = {
            "top": horizontal(image[:depth]),
            "bottom": horizontal(image[height - depth :]),
            "left": vertical(image[:, :depth]),
            "right": vertical(image[:, width - depth :]),
        }
        return {f"{edge}_{i}": color for edge, values in colors.items() for i, color in enumerate(values)}
