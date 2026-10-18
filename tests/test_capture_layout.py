"""Multi-monitor capture: stream ordering, target sizes and frame combining."""

import numpy as np

from glowcast.capture import ScreenCapture


def capture_with(streams, scale=0.125) -> ScreenCapture:
    capture = ScreenCapture(scale_factor=scale)
    capture._set_streams(streams)
    return capture


def solid(color, height=180, width=320):
    frame = np.zeros((height, width, 3), np.uint8)
    frame[...] = color
    return frame


BLUE, RED = (0, 0, 255), (255, 0, 0)


def test_side_by_side_monitors_are_ordered_left_to_right():
    # Portal order reversed on purpose: right monitor first
    capture = capture_with(
        [
            (91, {"position": (2560, 0), "size": (1920, 1080)}),
            (90, {"position": (0, 0), "size": (2560, 1440)}),
        ]
    )
    assert [s.node_id for s in capture._streams] == [90, 91]
    assert capture._horizontal
    assert [s.target_size for s in capture._streams] == [(320, 180), (320, 180)]

    combined = capture._combine([solid(BLUE), solid(RED)])
    assert combined.shape == (180, 640, 3)
    assert tuple(combined[90, 10]) == BLUE
    assert tuple(combined[90, 630]) == RED


def test_mixed_resolutions_share_the_taller_screens_height():
    capture = capture_with(
        [
            (1, {"position": (0, 0), "size": (2560, 1440)}),
            (2, {"position": (2560, 360), "size": (3840, 2160)}),
        ]
    )
    assert [s.target_size for s in capture._streams] == [(476, 268), (476, 268)]


def test_stacked_monitors_combine_vertically():
    capture = capture_with(
        [(1, {"position": (0, 0), "size": (1920, 1080)}), (2, {"position": (0, 1080), "size": (1920, 1080)})]
    )
    assert not capture._horizontal
    assert capture._combine([solid(BLUE), solid(RED)]).shape == (360, 320, 3)


def test_single_monitor_is_passed_through():
    capture = capture_with([(5, {"size": (2560, 1440)})])
    frame = solid(BLUE)
    assert capture._streams[0].target_size == (320, 180)
    assert capture._combine([frame]) is frame


def test_unscaled_fallback_frames_are_resized_to_match():
    capture = capture_with(
        [
            (90, {"position": (0, 0), "size": (2560, 1440)}),
            (91, {"position": (2560, 0), "size": (2560, 1440)}),
        ]
    )
    big_rgba = np.zeros((1440, 2560, 4), np.uint8)
    assert capture._combine([big_rgba, solid(RED)]).shape == (180, 640, 3)
