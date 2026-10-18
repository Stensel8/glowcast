"""Entertainment streaming against a local `openssl s_server` acting as the bridge."""

import shutil
import subprocess
import time

import pytest

import glowcast.entertainment as ent
from glowcast.entertainment import EntertainmentStream

PSK = "00112233445566778899aabbccddeeff"
APP_ID = "test-id"
CONFIG_ID = "25834667-7347-497d-ad1b-eaf83eecd7cb"

pytestmark = pytest.mark.skipif(not shutil.which("openssl"), reason="needs the openssl command")


class FakeBridge:
    def __init__(self, status="inactive", streamer=None):
        self.status = status
        self.streamer = streamer
        self.calls = []

    def get_entertainment_configuration(self, config_id):
        return {
            "id": config_id,
            "status": self.status,
            "active_streamer": {"rid": self.streamer, "rtype": "auth_v1"} if self.streamer else None,
            "channels": [
                {"channel_id": i, "position": {}, "members": [{"service": {"rid": f"l{i}"}}]}
                for i in range(3)
            ],
        }

    def get_application_id(self):
        return APP_ID

    def activate_entertainment_streaming(self, config_id):
        self.calls.append("start")
        return True

    def deactivate_entertainment_streaming(self, config_id):
        self.calls.append("stop")
        return True


@pytest.fixture
def server_port(monkeypatch, unused_udp_port):
    monkeypatch.setattr(ent.HueStreamProtocol, "PORT", unused_udp_port)
    monkeypatch.setattr(ent.HueStreamProtocol, "RESTART_DELAY", 0.05)
    return unused_udp_port


@pytest.fixture
def unused_udp_port():
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture
def bridge_server(server_port, tmp_path):
    """DTLS-PSK server; whatever it decrypts is written to a file."""
    received = tmp_path / "received.bin"
    command = [
        "openssl",
        "s_server",
        "-dtls1_2",
        "-nocert",
        "-psk",
        PSK,
        "-psk_identity",
        APP_ID,
        "-cipher",
        "PSK-AES128-GCM-SHA256",
        "-accept",
        f"127.0.0.1:{server_port}",
        "-quiet",
    ]
    if shutil.which("stdbuf"):
        command = ["stdbuf", "-o0", *command]  # so data is on disk before we kill it
    with open(received, "wb") as out:
        # stdin must stay open: s_server shuts down on EOF
        process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=out, stderr=subprocess.DEVNULL)
    time.sleep(0.4)
    yield received
    process.kill()
    process.wait()


def stream(key=PSK) -> EntertainmentStream:
    return EntertainmentStream("127.0.0.1", "app-key", key, CONFIG_ID, connection_timeout=0.05)


def test_good_key_connects_and_streams(bridge_server):
    s, bridge = stream(), FakeBridge()
    assert s.connect(bridge), s.last_error
    s.send_colors_xy({0: ((0.3, 0.3), 200)})
    time.sleep(0.5)
    s.disconnect(bridge)
    assert bridge.calls == ["start", "stop"]
    if shutil.which("stdbuf"):
        assert bridge_server.read_bytes().startswith(b"HueStream\x02\x00")


def test_wrong_key_retries_once_then_cleans_up(bridge_server):
    s, bridge = stream(key="ff" * 16), FakeBridge()
    assert not s.connect(bridge)
    assert "did not answer" in s.last_error
    assert s._openssl_proc is None
    assert bridge.calls == ["start", "stop", "start", "stop"]


def test_no_listener_fails_fast(server_port):
    s = stream()
    started = time.monotonic()
    assert not s.connect(FakeBridge())
    assert time.monotonic() - started < 2
    assert "rejected" in s.last_error


def test_zone_used_by_another_app_is_left_alone(server_port):
    s, bridge = stream(), FakeBridge(status="active", streamer="someone-else")
    assert not s.connect(bridge)
    assert "in use" in s.last_error
    assert bridge.calls == []


def test_stale_session_of_our_own_app_is_restarted(bridge_server):
    s, bridge = stream(), FakeBridge(status="active", streamer=APP_ID)
    assert s.connect(bridge), s.last_error
    s.disconnect(bridge)


def test_stream_ended_by_another_app_is_not_grabbed_back(server_port):
    """The Hue app's "stop" ends our session while we stream: the lights are theirs."""
    s, bridge = stream(), FakeBridge(status="inactive")
    s._connected, s._application_id = True, APP_ID
    s._last_send = time.monotonic()  # we were sending when it ended
    assert not s.reconnect_if_dropped(bridge)
    assert s.taken_over and "taken over" in s.last_error
    assert bridge.calls == []  # neither restarted nor stopped: not ours any more


def test_idle_timeout_still_reconnects(server_port):
    """The bridge drops sessions after ~10 s without data (e.g. picking a monitor)."""
    s, bridge = stream(), FakeBridge(status="inactive")
    s._connected, s._last_send = True, 0.0  # nothing sent for a long time
    s.reconnect_if_dropped(bridge)
    assert not s.taken_over and bridge.calls[0] == "start"
