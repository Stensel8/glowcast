"""GPU scaling must keep colour channels in order (needs a VA-API device)."""

import os

import pytest

from glowcast.capture import ScreenCapture

Gst = pytest.importorskip("gi.repository.Gst")
Gst.init(None)

pytestmark = pytest.mark.skipif(
    not os.path.exists("/dev/dri") or Gst.ElementFactory.find("vapostproc") is None,
    reason="needs a GPU with VA-API",
)


def test_vapostproc_output_keeps_blue_blue():
    capture = ScreenCapture()
    capture._set_streams([(1, {"size": (640, 360)})])
    capture._pipewire_fd = 0
    va_config = next(c for c in capture._get_pipeline_configs(capture._streams[0]) if "vapostproc" in c)
    output_caps = va_config.split(" ! ")[3]  # the caps after vapostproc
    pipeline = Gst.parse_launch(
        "videotestsrc num-buffers=1 pattern=solid-color foreground-color=0xFF0000FF ! "
        f"video/x-raw,format=BGRx,width=640,height=360 ! vapostproc ! {output_caps} ! "
        "appsink name=sink sync=false"
    )
    pipeline.set_state(Gst.State.PLAYING)
    sample = pipeline.get_by_name("sink").emit("try-pull-sample", 5 * Gst.SECOND)
    pipeline.set_state(Gst.State.NULL)
    if sample is None:
        pytest.skip("VA-API device not usable here")

    buffer = sample.get_buffer()
    _ok, info = buffer.map(Gst.MapFlags.READ)
    first_pixel = bytes(info.data[:4])
    buffer.unmap(info)
    fmt = sample.get_caps().get_structure(0).get_value("format")
    channels = dict(zip(fmt[:4], first_pixel))  # e.g. "BGRA" -> {"B": 255, ...}
    assert (channels["R"], channels["G"], channels["B"]) == (0, 0, 255)
