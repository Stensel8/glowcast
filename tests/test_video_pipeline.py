"""Video path: screen image -> trimean edge zones -> video engine -> light colours."""

import time
from types import SimpleNamespace

import numpy as np
import pytest

from glowcast.capture import ScreenCapture
from glowcast.colors import D65_XY, WARM_WHITE_XY, VideoEngine, srgb_to_linear, with_background_light
from glowcast.presets import GAME_PRESETS, VIDEO_PRESETS
from glowcast.sync import SyncController
from glowcast.zones import ZoneProcessor

MODERATE = VIDEO_PRESETS["moderate"]
RED = srgb_to_linear(np.array([1.0, 0.0, 0.0]))


def test_trimean_zones_ignore_small_bright_details():
    frame = np.zeros((180, 320, 3), np.uint8)
    frame[:22, :, 0] = 200  # red top edge
    frame[2:6, 2:6] = 255  # a white cursor in the first zone
    frame[-22:, :, 2] = 255  # blue bottom edge
    zones = ZoneProcessor().process_image(frame)
    r, g, b = zones["top_0"]
    assert r == pytest.approx(200) and g < 1 and b < 1
    assert zones["bottom_5"][2] == pytest.approx(255)
    assert len(zones) == 2 * 16 + 2 * 16


def test_engine_reaches_the_screen_colour_at_the_chosen_brightness():
    engine = VideoEngine()
    (x, y), brightness = engine.process({0: RED}, MODERATE, dt=1.0, brightness_scale=0.5)[0]
    assert (x, y) == pytest.approx((0.64, 0.33), abs=0.01)
    assert brightness == 127


def test_lights_glide_between_frames():
    engine = VideoEngine()
    engine.process({0: np.zeros(3)}, MODERATE, dt=1.0)
    steps = [engine.process({0: RED}, MODERATE, dt=0.02)[0][1] for _ in range(5)]
    assert steps == sorted(steps) and 0 < steps[0] < steps[-1] < 254


def test_fading_to_black_keeps_the_hue():
    engine = VideoEngine()
    engine.process({0: RED}, MODERATE, dt=1.0)
    (x, y), brightness = engine.process({0: np.full(3, 0.001)}, MODERATE, dt=0.05)[0]
    assert (x, y) == pytest.approx((0.64, 0.33), abs=0.02) and 0 < brightness < 254
    assert engine.process({0: np.zeros(3)}, MODERATE, dt=10.0)[0][1] == 0


def test_dark_scenes_change_more_slowly():
    def progress_after_one_frame(level):
        target = np.full(3, level)
        final = VideoEngine().process({0: target}, MODERATE, dt=10.0)[0][1]
        engine = VideoEngine()
        engine.process({0: np.zeros(3)}, MODERATE, dt=1.0)
        return engine.process({0: target}, MODERATE, dt=0.02)[0][1] / final

    assert progress_after_one_frame(0.03) < progress_after_one_frame(1.0)


def test_near_grey_shows_neutral_white():
    engine = VideoEngine()
    grey = srgb_to_linear(np.array([0.52, 0.5, 0.5]))
    (x, y), _ = engine.process({0: grey}, MODERATE, dt=1.0)[0]
    assert (x, y) == pytest.approx(D65_XY, abs=0.003)


def test_games_react_faster_than_video():
    def after_one_frame(preset):
        engine = VideoEngine()
        engine.process({0: np.zeros(3)}, preset, dt=1.0)
        return engine.process({0: RED}, preset, dt=0.02)[0][1]

    assert after_one_frame(GAME_PRESETS["moderate"]) > after_one_frame(VIDEO_PRESETS["moderate"])


def test_background_light_turns_darkness_into_warm_white():
    assert with_background_light(((0.64, 0.33), 0), 30) == (WARM_WHITE_XY, 30)
    assert with_background_light(((0.64, 0.33), 200), 30) == ((0.64, 0.33), 200)


def test_sync_delay_holds_colours_back(monkeypatch):
    controller = SyncController(None, None, ZoneProcessor(), settings=None)
    clock = iter([0.0, 0.1, 0.25, 0.35])
    monkeypatch.setattr("glowcast.sync.time.monotonic", lambda: next(clock))
    assert controller._delay({"a": 1}, 0.2) is None
    assert controller._delay({"b": 2}, 0.2) is None
    assert controller._delay({"c": 3}, 0.2) == {"a": 1}
    assert controller._delay({"d": 4}, 0.2) == {"b": 2}
    assert controller._delay({"e": 5}, 0.0) == {"e": 5}


def test_capture_area_crops_the_combined_screen():
    capture = ScreenCapture()
    screen = np.zeros((100, 200, 3), np.uint8)
    assert capture._crop_to_capture_area(screen) is screen
    capture.capture_area = (0.5, 0.0, 1.0, 0.5)
    assert capture._crop_to_capture_area(screen).shape == (50, 100, 3)


def test_xy_to_rgb_gives_the_light_colour_for_the_glow():
    from glowcast.colors import xy_to_rgb

    assert xy_to_rgb(0.64, 0.33) == pytest.approx((1.0, 0.0, 0.0), abs=0.02)
    assert xy_to_rgb(*D65_XY) == pytest.approx((1.0, 1.0, 1.0), abs=0.02)


class SlowCapture:
    """A screen share that takes a while to open, like the portal can."""

    def __init__(self, delay_s):
        self.delay_s, self.capture_area, self.last_full_frame = delay_s, None, None
        self.stops, self.active = 0, 0

    def capture(self):
        self.active += 1
        time.sleep(self.delay_s)
        self.active -= 1

    def stop_pipeline(self):
        self.stops += 1


def sync_with(capture):
    settings = SimpleNamespace(
        video_settings=lambda mode: SimpleNamespace(
            intensity="moderate", use_audio=False, background_light=False
        ),
        capture=SimpleNamespace(capture_area=[0, 0, 1, 1], sync_delay_ms=0),
    )
    return SyncController(None, capture, ZoneProcessor(), settings)


def test_a_slow_screen_share_never_leaves_two_loops_running(monkeypatch):
    monkeypatch.setattr(SyncController, "STOP_TIMEOUT_S", 0.05)
    capture = SlowCapture(0.4)
    controller = sync_with(capture)
    controller.start()
    time.sleep(0.05)
    controller.stop()  # times out: the old loop is still opening the screen share
    controller.start()  # the user picked Video again right away
    time.sleep(0.9)
    assert capture.active <= 1  # only the new loop captures
    assert capture.stops == 0  # the outdated loop left the new one's session alone
    SyncController.STOP_TIMEOUT_S = 2.0
    controller.stop()
    assert capture.stops == 1


def test_unplaced_lights_follow_the_screen_average():
    positions = {0: (0, 0), 1: (0, 0), 2: (0, 0), 3: (-0.04, -0.41), 4: (-0.74, 1.0), 5: (-0.10, 1.0)}
    stream = SimpleNamespace(
        get_channel_positions=lambda: {c: {"x": x, "y": 0, "z": z} for c, (x, z) in positions.items()},
    )
    controller = SyncController(None, None, ZoneProcessor(), settings=None, entertainment_stream=stream)
    controller._build_zone_channel_mapping()
    assert controller._unmapped_channels == [1, 2]

    zones = {
        z: np.array([255.0, 0, 0]) if z.startswith("top") else np.zeros(3) for z in controller._zone_channel
    }
    colors = controller._channel_colors(zones)
    assert sorted(colors) == [0, 1, 2, 3, 4, 5]
    assert colors[4][0] > 0.2 and not colors[4][1:].any()  # top-left light: red
    assert 0 < colors[1][0] < colors[4][0]  # unplaced: the (dimmer) screen average
    assert not colors[3].any()  # only black zones
