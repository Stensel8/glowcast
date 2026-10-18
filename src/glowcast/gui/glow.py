"""The lights glowing in the window, like Hue Sync's tinted, glowing card.

Each light shines where it stands in the entertainment area, in the colour
and at the brightness it was last sent, so the window pulses with the bulbs
and the brightness slider dims both. Kept soft so the app stays readable.
Drawn by the GPU (GSK gradients) and eased every display frame.
"""

import math
from collections.abc import Callable

import gi

gi.require_version("Gsk", "4.0")
gi.require_version("Graphene", "1.0")
from gi.repository import Gdk, GLib, Graphene, Gsk, Gtk

Light = tuple[float, float, tuple[float, float, float], float]  # x, z (-1..1), rgb (0-1), level (0-1)

EASE_S = 0.07  # quick enough to bounce with the beat, smooth enough not to jitter
FRAME_S = 1 / 60  # redraws at most this often (lights update 50 times a second)
WINDOW_STRENGTH = 0.16  # glow opacity behind the window at full brightness
AMBIENT_STRENGTH = 0.06  # the whole window takes on the lights' average colour
CARD_STRENGTH = 0.32  # tint of the status card at full brightness
SOFTEN = 0.3  # glow colours move this far towards grey, for readability
SPREAD = 0.3  # lights placed at the same spot are fanned out this far, left to right


def _stop(offset: float, rgb, alpha: float) -> Gsk.ColorStop:
    stop = Gsk.ColorStop()
    stop.offset = offset
    color = Gdk.RGBA()
    color.red, color.green, color.blue, color.alpha = *rgb, alpha
    stop.color = color
    return stop


def _soft(r: float, g: float, b: float) -> tuple[float, float, float]:
    grey = 0.2126 * r + 0.7152 * g + 0.0722 * b
    return tuple(c + (grey - c) * SOFTEN for c in (r, g, b))


def _spread(lights: list[Light]) -> list[Light]:
    """Fan out lights that share a spot (e.g. left at the origin in the Hue app)."""
    groups: dict[tuple[float, float], list[int]] = {}
    for i, (x, z, _rgb, _level) in enumerate(lights):
        groups.setdefault((round(x, 2), round(z, 2)), []).append(i)
    spread = list(lights)
    for members in groups.values():
        for n, i in enumerate(members if len(members) > 1 else []):
            x, z, rgb, level = lights[i]
            spread[i] = (x - SPREAD + 2 * SPREAD * n / (len(members) - 1), z, rgb, level)
    return spread


class _Easing:
    """Mixin for widgets: eases towards the lights from source() on every frame."""

    def _init_glow(self, source: Callable[[], list[Light]]) -> None:
        self._source = source
        self._lights: list[list[float]] = []  # [x, z, r, g, b, level], eased
        self._last_time: float | None = None
        self._last_draw = 0.0
        self._tick_id = 0
        self.connect("map", lambda _w: self._start())
        self.connect("unmap", lambda _w: self._stop())

    def _start(self) -> None:
        if not self._tick_id:
            self._last_time = None
            self._tick_id = self.add_tick_callback(self._on_tick)

    def _stop(self) -> None:
        if self._tick_id:
            self.remove_tick_callback(self._tick_id)
            self._tick_id = 0

    def _visible_lights(self) -> list[list[float]]:
        return [light for light in self._lights if light[5] >= 0.005]

    def _on_tick(self, _widget, clock) -> bool:
        now = clock.get_frame_time() / 1_000_000
        if self._last_time is not None and now - self._last_draw < FRAME_S:
            return GLib.SOURCE_CONTINUE  # high refresh-rate screens: no need to redraw every frame
        dt = 0.0 if self._last_time is None else now - self._last_time
        self._last_time = self._last_draw = now
        targets = _spread(sorted(self._source(), key=lambda light: (light[0], light[1])))
        lights = self._lights
        while len(lights) < len(targets):  # new lights grow from nothing
            x, z, rgb, _level = targets[len(lights)]
            lights.append([x, z, *rgb, 0.0])
        step = 1 - math.exp(-dt / EASE_S) if dt else 1.0
        for i, light in enumerate(lights):
            x, z, rgb, level = targets[i] if i < len(targets) else (*light[:2], light[2:5], 0.0)
            for j, value in enumerate((x, z, *rgb, level)):
                light[j] += (value - light[j]) * step
        if len(lights) > len(targets) and all(light[5] < 0.005 for light in lights[len(targets) :]):
            del lights[len(targets) :]
        if lights or targets:
            self.queue_draw()
        return GLib.SOURCE_CONTINUE


class WindowGlow(_Easing, Gtk.Widget):
    """Soft light behind the whole window, one glow per bulb."""

    def __init__(self, source: Callable[[], list[Light]]):
        Gtk.Widget.__init__(self, hexpand=True, vexpand=True, can_target=False, can_focus=False)
        self._init_glow(source)

    def do_snapshot(self, snapshot) -> None:
        lights = self._visible_lights()
        if not lights:
            return
        width, height = self.get_width(), self.get_height()
        # A wash over the whole window in the lights' average colour: the app pulses with them
        total = sum(light[5] for light in lights)
        wash = Gdk.RGBA()
        wash.red, wash.green, wash.blue = _soft(
            *(sum(light[2 + i] * light[5] for light in lights) / total for i in range(3))
        )
        wash.alpha = AMBIENT_STRENGTH * total / len(lights)
        snapshot.append_color(wash, Graphene.Rect().init(0, 0, width, height))
        # Each light glows where it stands: ceiling lights at the top, floor lights lower down
        for x, z, r, g, b, level in lights:
            cx = (min(1.0, max(-1.0, x)) + 1) / 2 * width
            cy = height * (0.08 + 0.84 * (1 - (min(1.0, max(-1.0, z)) + 1) / 2))
            breathe = 0.75 + 0.35 * level  # glows grow and shrink with the brightness
            h_radius, v_radius = width * 0.45 * breathe, height * 0.4 * breathe
            bounds = Graphene.Rect().init(cx - h_radius, cy - v_radius, 2 * h_radius, 2 * v_radius)
            center = Graphene.Point().init(cx, cy)
            rgb, alpha = _soft(r, g, b), WINDOW_STRENGTH * level
            # roughly bell-shaped, so glows fade out without an edge
            bell = ((0.0, 1.0), (0.35, 0.6), (0.7, 0.15), (1.0, 0.0))
            stops = [_stop(t, rgb, alpha * f) for t, f in bell]
            snapshot.append_radial_gradient(bounds, center, h_radius, v_radius, 0.0, 1.0, stops)


class GlowCard(_Easing, Gtk.Box):
    """A box tinted with the lights' colours from left to right, like Hue Sync's top card.

    The tint is painted between the box's CSS background and its children,
    so the box sizes like any other.
    """

    RADIUS = 12  # matches .status-card

    def __init__(self, source: Callable[[], list[Light]], **box_props):
        Gtk.Box.__init__(self, **box_props)
        self._init_glow(source)

    def do_snapshot(self, snapshot) -> None:
        lights = self._visible_lights()
        if lights:
            self._paint_tint(snapshot, lights)
        Gtk.Box.do_snapshot(self, snapshot)

    def _paint_tint(self, snapshot, lights) -> None:
        width, height = self.get_width(), self.get_height()
        bounds = Graphene.Rect().init(0, 0, width, height)
        corner = Graphene.Size().init(self.RADIUS, self.RADIUS)
        snapshot.push_rounded_clip(Gsk.RoundedRect().init(bounds, corner, corner, corner, corner))
        # Lights close together blend into one stop, so the gradient has no hard stripes
        merged: list[list[float]] = []  # [offset, r, g, b, level, weight]
        for x, _z, r, g, b, level in lights:
            offset = (min(1.0, max(-1.0, x)) + 1) / 2
            if merged and offset - merged[-1][0] < 0.12:
                last = merged[-1]
                w = last[5]
                last[:5] = [(v * w + n) / (w + 1) for v, n in zip(last[:5], (offset, r, g, b, level))]
                last[5] = w + 1
            else:
                merged.append([offset, r, g, b, level, 1])
        if len(merged) == 1:  # one colour: tint the whole card
            merged = [[0.0, *merged[0][1:]], [1.0, *merged[0][1:]]]
        stops = [_stop(o, _soft(r, g, b), CARD_STRENGTH * level) for o, r, g, b, level, _w in merged]
        start, end = Graphene.Point().init(0, 0), Graphene.Point().init(width, 0)
        snapshot.append_linear_gradient(bounds, start, end, stops)
        snapshot.pop()
