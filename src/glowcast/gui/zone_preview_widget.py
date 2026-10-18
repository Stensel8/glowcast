"""Live preview: the captured screen with its edge zone colours around it.

Dragging across the screen sets the capture area. Hue Sync draws that on the
screen itself with keyboard shortcuts; Wayland has no such overlay, and here
you see exactly what is captured.
"""

import math
from collections.abc import Callable

import cairo
import numpy as np
from gi.repository import Gtk

FULL_AREA = (0.0, 0.0, 1.0, 1.0)
MAX_PREVIEW_WIDTH = 640  # frames are subsampled to about this for drawing


class ZonePreviewWidget(Gtk.DrawingArea):
    __gtype_name__ = "ZonePreviewWidget"

    def __init__(self, rows: int, cols: int, on_area_changed: Callable[[tuple], None]):
        super().__init__(hexpand=True)
        self.rows, self.cols = rows, cols
        self._on_area_changed = on_area_changed
        self._zones: dict[str, tuple] = {}
        self._surface = None  # the latest frame as a cairo surface
        self._pixels = None  # keeps the surface's memory alive
        self.capture_area = FULL_AREA
        self._drag: tuple | None = None  # (x0, y0, x1, y1) in widget pixels
        self.set_draw_func(self._draw)
        self.set_cursor_from_name("crosshair")
        self.set_tooltip_text("Drag across the screen to sync only that part")

        drag = Gtk.GestureDrag()
        drag.connect("drag-begin", self._on_drag_begin)
        drag.connect("drag-update", self._on_drag_update)
        drag.connect("drag-end", self._on_drag_end)
        self.add_controller(drag)

    def update(self, zones: dict, frame: np.ndarray | None) -> None:
        self._zones = {z: tuple(int(v) for v in rgb[:3]) for z, rgb in zones.items()}
        self._set_frame(frame)
        self.queue_draw()

    def clear(self) -> None:
        self._zones, self._surface, self._pixels = {}, None, None
        self.queue_draw()

    def set_capture_area(self, area) -> None:
        self.capture_area = tuple(area)
        self.queue_draw()

    def _set_frame(self, frame: np.ndarray | None) -> None:
        if frame is None or frame.ndim != 3:
            return
        step = max(1, math.ceil(frame.shape[1] / MAX_PREVIEW_WIDTH))
        rgb = frame[::step, ::step, :3]
        height, width = rgb.shape[:2]
        stride = cairo.ImageSurface.format_stride_for_width(cairo.FORMAT_RGB24, width)
        pixels = np.zeros((height, stride // 4, 4), dtype=np.uint8)
        pixels[:, :width, :3] = rgb[:, :, ::-1]  # cairo wants B, G, R, x
        self._pixels = pixels
        self._surface = cairo.ImageSurface.create_for_data(
            pixels.data, cairo.FORMAT_RGB24, width, height, stride
        )

    # --- geometry --------------------------------------------------------------

    def _edge(self, height: int) -> int:
        return min(36, height // 6)

    def _screen_rect(self, width: int, height: int) -> tuple[float, float, float, float]:
        """Where the screen is drawn, keeping the frame's aspect ratio."""
        pad = self._edge(height) + 6
        x, y, w, h = pad, pad, width - 2 * pad, height - 2 * pad
        if self._surface is not None and w > 0 and h > 0:
            aspect = self._surface.get_width() / self._surface.get_height()
            if w / h > aspect:
                x, w = x + (w - h * aspect) / 2, h * aspect
            else:
                y, h = y + (h - w / aspect) / 2, w / aspect
        return x, y, w, h

    # --- drawing ---------------------------------------------------------------

    def _draw(self, _area, ctx, width, height):
        ctx.set_source_rgb(0.08, 0.08, 0.08)
        ctx.paint()
        self._draw_zones(ctx, width, height)

        x, y, w, h = self._screen_rect(width, height)
        ctx.rectangle(x, y, w, h)
        if self._surface is not None:
            ctx.save()
            ctx.translate(x, y)
            ctx.scale(w / self._surface.get_width(), h / self._surface.get_height())
            ctx.set_source_surface(self._surface, 0, 0)
            ctx.get_source().set_filter(cairo.FILTER_GOOD)
            ctx.paint()
            ctx.restore()
        else:
            ctx.set_source_rgb(0.04, 0.04, 0.06)
            ctx.fill()

        area = self._drag_area(width, height) if self._drag else self.capture_area
        if area != FULL_AREA:
            ax, ay = x + area[0] * w, y + area[1] * h
            aw, ah = (area[2] - area[0]) * w, (area[3] - area[1]) * h
            ctx.set_fill_rule(cairo.FILL_RULE_EVEN_ODD)
            ctx.rectangle(x, y, w, h)
            ctx.rectangle(ax, ay, aw, ah)
            ctx.set_source_rgba(0, 0, 0, 0.6)
            ctx.fill()
            ctx.rectangle(ax, ay, aw, ah)
            ctx.set_source_rgb(0.21, 0.52, 0.89)
            ctx.set_line_width(2)
            ctx.stroke()

    def _draw_zones(self, ctx, width, height):
        edge, gap = self._edge(height), 2
        inner = height - 2 * edge - 2 * gap
        cells = []
        zone_w = (width - (self.cols + 1) * gap) / self.cols
        for i in range(self.cols):
            x = gap + i * (zone_w + gap)
            cells.append((f"top_{i}", x, gap, zone_w, edge - gap))
            cells.append((f"bottom_{i}", x, height - edge, zone_w, edge - gap))
        zone_h = (inner - (self.rows - 1) * gap) / self.rows
        for i in range(self.rows):
            y = edge + gap + i * (zone_h + gap)
            cells.append((f"left_{i}", gap, y, edge - gap, zone_h))
            cells.append((f"right_{i}", width - edge, y, edge - gap, zone_h))
        for zone_id, x, y, w, h in cells:
            r, g, b = (c / 255 for c in self._zones.get(zone_id, (30, 30, 30)))
            ctx.rectangle(x, y, w, h)
            ctx.set_source_rgb(r, g, b)
            ctx.fill()

    # --- capture area ----------------------------------------------------------

    def _drag_area(self, width, height) -> tuple:
        x, y, w, h = self._screen_rect(width, height)
        x0, y0, x1, y1 = self._drag
        nx = sorted(min(1.0, max(0.0, (v - x) / w)) for v in (x0, x1))
        ny = sorted(min(1.0, max(0.0, (v - y) / h)) for v in (y0, y1))
        return nx[0], ny[0], nx[1], ny[1]

    def _on_drag_begin(self, _gesture, x, y):
        self._drag = (x, y, x, y)

    def _on_drag_update(self, _gesture, dx, dy):
        x0, y0, _x1, _y1 = self._drag
        self._drag = (x0, y0, x0 + dx, y0 + dy)
        self.queue_draw()

    def _on_drag_end(self, _gesture, _dx, _dy):
        area = self._drag_area(self.get_width(), self.get_height())
        self._drag = None
        if area[2] - area[0] >= 0.05 and area[3] - area[1] >= 0.05:
            self.capture_area = area
            self._on_area_changed(area)
        self.queue_draw()
