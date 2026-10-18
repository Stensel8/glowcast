"""Desktop audio capture and spectrum analysis via GStreamer.

Listens to what the speakers play (the default sink's monitor) and yields a
spectrum plus per-channel loudness every UPDATE_INTERVAL_MS. Meant to be read
from a worker thread: read() polls the pipeline bus directly, so no GLib main
loop is involved.
"""

import re
from dataclasses import dataclass

import gi

gi.require_version("Gst", "1.0")
from gi.repository import GLib, Gst

Gst.init(None)

SAMPLE_RATE = 16000
SPECTRUM_BANDS = 256  # 31.25 Hz per band, up to 8 kHz
BAND_WIDTH_HZ = SAMPLE_RATE / 2 / SPECTRUM_BANDS
UPDATE_INTERVAL_MS = 20  # 50 analysis frames per second
SPECTRUM_FLOOR_DB = -90.0
DEFAULT_DEVICE = "@DEFAULT_MONITOR@"  # what the default speakers play
DEFAULT_SOURCES = ((DEFAULT_DEVICE, "Default system output"), ("@DEFAULT_SOURCE@", "Default system input"))

# Element message fields are list types PyGObject cannot unpack without the
# gst-python overrides, so values are read from the structure's text form.
_NUMBER = re.compile(r"-?(?:inf|\d+(?:\.\d*)?(?:e[-+]?\d+)?)")


@dataclass
class AudioFrame:
    magnitudes_db: list[float]  # one value per spectrum band (stereo mixed)
    channel_rms_db: tuple[float, float]  # (left, right); equal for mono


def _field_values(structure_text: str, field: str, opener: str, closer: str) -> list[float]:
    start = structure_text.find(f"{field}=")
    if start < 0:
        return []
    start = structure_text.find(opener, start)
    end = structure_text.find(closer, start)
    return [float(v) for v in _NUMBER.findall(structure_text[start + 1 : end])]


def list_sources() -> list[tuple[str, str]]:
    """(PulseAudio source name, label): speaker outputs first, then inputs.

    An output is listened to through its monitor source, like Hue Sync's
    loopback capture on Windows.
    """
    monitor = Gst.DeviceMonitor.new()
    monitor.add_filter("Audio/Sink", None)
    monitor.add_filter("Audio/Source", None)
    if not monitor.start():
        return list(DEFAULT_SOURCES)
    outputs, inputs = [], []
    try:
        for device in monitor.get_devices():
            try:
                name = device.get_property("internal-name")  # PulseAudio provider
            except TypeError:
                props = device.get_properties()
                name = props.get_string("node.name") if props else None  # PipeWire provider
            if not name or name.endswith(".monitor"):
                continue
            if device.has_classes("Audio/Sink"):
                outputs.append((f"{name}.monitor", device.get_display_name()))
            else:
                inputs.append((name, f"{device.get_display_name()} (input)"))
    finally:
        monitor.stop()
    return list(DEFAULT_SOURCES) + sorted(outputs, key=lambda s: s[1]) + sorted(inputs, key=lambda s: s[1])


class AudioCapture:
    """GStreamer pipeline: pulsesrc ! level ! [audiokaraoke] ! spectrum ! fakesink."""

    def __init__(self, device: str = DEFAULT_DEVICE, vocal_filter: bool = False):
        self.device = device or DEFAULT_DEVICE
        # Removes what is mixed to the centre (usually the singer), so the
        # lights follow the instruments. Stereo levels are measured before it.
        self.vocal_filter = vocal_filter
        self.last_error = ""
        self._pipeline: Gst.Pipeline | None = None
        self._bus: Gst.Bus | None = None
        self._rms_db = (SPECTRUM_FLOOR_DB, SPECTRUM_FLOOR_DB)

    def start(self) -> bool:
        interval_ns = UPDATE_INTERVAL_MS * Gst.MSECOND
        description = (
            f'pulsesrc device="{self.device}" client-name=Glowcast '
            "buffer-time=40000 latency-time=10000 ! "
            "audioconvert ! audioresample ! "
            f"audio/x-raw,format=F32LE,channels=2,rate={SAMPLE_RATE} ! "
            f"level interval={interval_ns} post-messages=true ! "
            + ("audiokaraoke ! " if self.vocal_filter else "")
            + f"spectrum bands={SPECTRUM_BANDS} interval={interval_ns} "
            f"threshold={int(SPECTRUM_FLOOR_DB)} post-messages=true message-magnitude=true ! "
            "fakesink sync=false async=false"
        )
        try:
            self._pipeline = Gst.parse_launch(description)
        except GLib.Error as e:
            self.last_error = f"Could not build audio pipeline: {e.message}"
            return False

        self._bus = self._pipeline.get_bus()
        if self._pipeline.set_state(Gst.State.PLAYING) == Gst.StateChangeReturn.FAILURE:
            self.last_error = self._pop_error() or "Could not open desktop audio"
            self.stop()
            return False
        return True

    def read(self, timeout_s: float) -> AudioFrame | None:
        """Return the next spectrum frame, or None on timeout or error.

        On error last_error is set and the pipeline is stopped.
        """
        if not self._bus:
            return None
        message_types = Gst.MessageType.ELEMENT | Gst.MessageType.ERROR
        timeout_ns = int(timeout_s * Gst.SECOND)
        while True:
            message = self._bus.timed_pop_filtered(timeout_ns, message_types)
            if message is None:
                return None
            if message.type == Gst.MessageType.ERROR:
                err, _debug = message.parse_error()
                self.last_error = err.message
                self.stop()
                return None

            structure = message.get_structure()
            name = structure.get_name() if structure else ""
            if name == "level":
                rms = _field_values(structure.to_string(), "rms", "<", ">")
                if rms:
                    self._rms_db = (rms[0], rms[-1])
            elif name == "spectrum":
                magnitudes = _field_values(structure.to_string(), "magnitude", "{", "}")
                if magnitudes:
                    return AudioFrame(magnitudes, self._rms_db)

    def _pop_error(self) -> str:
        if not self._bus:
            return ""
        message = self._bus.pop_filtered(Gst.MessageType.ERROR)
        return message.parse_error()[0].message if message else ""

    def stop(self) -> None:
        if self._pipeline:
            self._pipeline.set_state(Gst.State.NULL)
        self._pipeline = None
        self._bus = None
