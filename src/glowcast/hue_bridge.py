"""The Hue bridge: finding it, pairing, and its CLIP v2 REST API.

Standard library only. The bridge serves HTTPS with its own certificate, so
local requests skip verification, as the Hue apps' local API clients do.
"""

import json
import socket
import ssl
import struct
import time
import urllib.error
import urllib.request
from dataclasses import dataclass

from glowcast.utils.logging import timed_print

TIMEOUT_S = 5.0
LINK_BUTTON_WAIT_S = 30.0  # how long pairing waits for the bridge's button
_LOCAL_TLS = ssl.create_default_context()
_LOCAL_TLS.check_hostname = False
_LOCAL_TLS.verify_mode = ssl.CERT_NONE


class BridgeError(Exception):
    """A bridge request failed (unreachable, refused, or an API error)."""


@dataclass(frozen=True)
class AreaLight:
    id: str
    name: str
    x: float  # -1 (left) to 1 (right), from the entertainment area layout
    gradient_points: int  # >1 for gradient lights (lightstrips, Play gradient)
    on: bool


def _http(method: str, url: str, payload=None, headers=None, tls=_LOCAL_TLS, timeout=TIMEOUT_S):
    """Send a request; returns (parsed JSON body, response headers)."""
    data = None if payload is None else json.dumps(payload).encode()
    request = urllib.request.Request(url, data=data, method=method, headers=headers or {})
    if data is not None:
        request.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(request, context=tls, timeout=timeout) as response:
            body = response.read()
            return (json.loads(body) if body else {}), response.headers
    except urllib.error.HTTPError as e:
        raise BridgeError(f"Bridge answered {e.code} for {url.split('/', 3)[-1]}") from e
    except (urllib.error.URLError, OSError, ValueError) as e:
        raise BridgeError(f"Cannot reach the bridge: {getattr(e, 'reason', e)}") from e


class HueBridge:
    def __init__(self, bridge_ip: str, app_key: str):
        self.bridge_ip = bridge_ip
        self.app_key = app_key

    def update_credentials(self, bridge_ip: str, app_key: str) -> None:
        self.bridge_ip, self.app_key = bridge_ip, app_key

    def _request(self, method: str, path: str, payload=None) -> dict:
        if not self.bridge_ip or not self.app_key:
            raise BridgeError("The bridge is not set up")
        body, _headers = _http(
            method,
            f"https://{self.bridge_ip}/clip/v2{path}",
            payload,
            {"hue-application-key": self.app_key},
        )
        return body

    def get_resources(self, rtype: str) -> list[dict]:
        try:
            return self._request("GET", f"/resource/{rtype}").get("data", [])
        except BridgeError as e:
            timed_print(f"Reading {rtype} from the bridge failed: {e}")
            return []

    def set_light_state(self, light_id: str, payload: dict) -> bool:
        try:
            self._request("PUT", f"/resource/light/{light_id}", payload)
            return True
        except BridgeError as e:
            timed_print(f"Setting light {light_id} failed: {e}")
            return False

    def test_connection(self) -> bool:
        try:
            self._request("GET", "/resource/bridge")
            return True
        except BridgeError:
            return False

    connect = test_connection

    # --- entertainment ---------------------------------------------------------

    @staticmethod
    def _summary(config: dict) -> dict:
        return {
            "id": config.get("id"),
            "name": config.get("metadata", {}).get("name", "Area"),
            "status": config.get("status"),
            "active_streamer": config.get("active_streamer"),
            "channels": config.get("channels", []),
            "locations": config.get("locations", {}),
        }

    def get_entertainment_configurations(self, raise_errors: bool = False) -> list[dict]:
        """The bridge's entertainment areas; [] if it can't be read, unless raise_errors."""
        if raise_errors:
            items = self._request("GET", "/resource/entertainment_configuration").get("data", [])
        else:
            items = self.get_resources("entertainment_configuration")
        return [self._summary(c) for c in items]

    def get_entertainment_configuration(self, config_id: str) -> dict | None:
        try:
            items = self._request("GET", f"/resource/entertainment_configuration/{config_id}").get("data", [])
        except BridgeError as e:
            timed_print(f"Reading the entertainment area failed: {e}")
            return None
        return self._summary(items[0]) if items else None

    def _set_streaming(self, config_id: str, action: str) -> bool:
        try:
            self._request("PUT", f"/resource/entertainment_configuration/{config_id}", {"action": action})
            timed_print(f"Entertainment streaming {action} for config {config_id}")
            return True
        except BridgeError as e:
            timed_print(f"Entertainment streaming {action} failed: {e}")
            return False

    def activate_entertainment_streaming(self, config_id: str) -> bool:
        return self._set_streaming(config_id, "start")

    def deactivate_entertainment_streaming(self, config_id: str) -> bool:
        return self._set_streaming(config_id, "stop")

    def get_application_id(self) -> str | None:
        """hue-application-id: the PSK identity for DTLS streaming."""
        if not self.bridge_ip or not self.app_key:
            return None
        try:
            _body, headers = _http(
                "GET", f"https://{self.bridge_ip}/auth/v1", headers={"hue-application-key": self.app_key}
            )
            return headers.get("hue-application-id")
        except BridgeError:
            return None

    def get_area_lights(self, config_id: str) -> list[AreaLight]:
        """The lights of an entertainment area, ordered from left to right.

        Channels reference entertainment services; each belongs to a device
        whose light service is what REST commands address.
        """
        config = self.get_entertainment_configuration(config_id) if config_id else None
        if not config:
            return []
        x_of_service: dict[str, float] = {}
        for channel in config["channels"]:
            x = float(channel.get("position", {}).get("x", 0.0))
            for member in channel.get("members", []):
                if rid := member.get("service", {}).get("rid"):
                    x_of_service.setdefault(rid, x)
        lights = {light["id"]: light for light in self.get_resources("light") if "id" in light}
        area: dict[str, AreaLight] = {}
        for device in self.get_resources("device"):
            services = device.get("services", [])
            xs = [x_of_service[s["rid"]] for s in services if s.get("rid") in x_of_service]
            for service in services if xs else []:
                light = lights.get(service.get("rid")) if service.get("rtype") == "light" else None
                if light and light["id"] not in area:
                    area[light["id"]] = AreaLight(
                        id=light["id"],
                        name=light.get("metadata", {}).get("name", ""),
                        x=xs[0],
                        gradient_points=light.get("gradient", {}).get("points_capable", 0),
                        on=light.get("on", {}).get("on", False),
                    )
        return sorted(area.values(), key=lambda light: (light.x, light.name))

    # --- finding and pairing ---------------------------------------------------

    @staticmethod
    def create_user(bridge_ip: str, wait_s: float = LINK_BUTTON_WAIT_S) -> dict | None:
        """Pair with the bridge: {"app_key", "client_key"} once its button is pressed.

        Keeps asking while the user walks to the bridge, like Hue Sync's
        "Press the push-link button" step. Blocks: call from a worker thread.
        """
        payload = {"devicetype": "glowcast#linux", "generateclientkey": True}
        deadline = time.monotonic() + wait_s
        while True:
            body, _headers = _http("POST", f"https://{bridge_ip}/api", payload)
            result = body[0] if isinstance(body, list) and body else {}
            if success := result.get("success"):
                return {"app_key": success["username"], "client_key": success.get("clientkey", "")}
            if result.get("error", {}).get("type") != 101:  # 101: button not pressed yet
                raise BridgeError(result.get("error", {}).get("description", "Pairing failed"))
            if time.monotonic() >= deadline:
                return None
            time.sleep(1.0)

    @staticmethod
    def discover_bridges(timeout: float = 5.0) -> list[str]:
        """Bridge addresses on the local network (mDNS), else from Signify's discovery service."""
        try:
            found = _discover_mdns(timeout)
        except OSError as e:
            timed_print(f"mDNS discovery failed: {e}")
            found = []
        if not found:
            try:
                body, _headers = _http(
                    "GET", "https://discovery.meethue.com/", tls=ssl.create_default_context()
                )
                found = [b["internalipaddress"] for b in body if b.get("internalipaddress")]
            except BridgeError as e:
                timed_print(f"Online bridge discovery failed: {e}")
        return found


def _discover_mdns(timeout: float) -> list[str]:
    """Ask the network who offers _hue._tcp (RFC 6762).

    Bridges answer by multicast only, so listen on the mDNS port next to the
    desktop's own responder (SO_REUSEPORT) instead of on a private port.
    """
    name = b"".join(bytes([len(label)]) + label for label in (b"_hue", b"_tcp", b"local")) + b"\0"
    query = struct.pack(">6H", 0, 0, 1, 0, 0, 0) + name + struct.pack(">2H", 12, 1)  # PTR, IN
    group = socket.inet_aton("224.0.0.251")
    found: list[str] = []
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
        sock.bind(("", 5353))
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, group + socket.inet_aton("0.0.0.0"))
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 255)
        sock.settimeout(0.3)
        start = time.monotonic()
        last_query = -1.0
        while (now := time.monotonic()) - start < timeout:
            if found and now - start > 1.5:
                break  # a second and a half is plenty for every bridge to answer
            if now - last_query >= 1.0:
                sock.sendto(query, ("224.0.0.251", 5353))
                last_query = now
            try:
                data, (address, _port) = sock.recvfrom(9000)
            except TimeoutError:
                continue
            is_answer = len(data) > 2 and data[2] & 0x80
            if is_answer and b"\x04_hue\x04_tcp" in data and address not in found:
                found.append(address)
    return found
