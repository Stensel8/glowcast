"""Scenes mode: static (or slowly moving) light on the entertainment area.

Like Hue Sync's Scenes: its gallery presets, your own scenes, and the scenes
stored on the bridge for rooms and zones that share lights with the area.
Only the area's lights are ever changed, also for a room scene that covers
more lights.
"""

import threading
from dataclasses import dataclass

from glowcast.colors import hex_to_xy, mirek_to_xy, xy_to_hex
from glowcast.hue_bridge import AreaLight, HueBridge
from glowcast.presets import SCENE_PRESETS, Scene
from glowcast.utils.logging import timed_print

TRANSITION_MS = 400
DYNAMIC_INTERVAL_S = 8.0  # with Dynamics on, colours move one light on this often
_LOOK_KEYS = ("color", "color_temperature", "gradient")


@dataclass(frozen=True)
class BridgeScene:
    id: str  # "bridge:<scene id>"
    name: str
    group: str  # name of the scene's room or zone
    colors: tuple[str, ...]  # for the swatch, and for lights the scene doesn't cover
    actions: dict  # light id -> the scene's action for that light


def _xy(hex_color: str) -> dict:
    x, y = hex_to_xy(hex_color)
    return {"xy": {"x": round(x, 4), "y": round(y, 4)}}


def _action_hex(action: dict) -> str:
    if xy := action.get("color", {}).get("xy"):
        return xy_to_hex(xy["x"], xy["y"])
    if mirek := action.get("color_temperature", {}).get("mirek"):
        return xy_to_hex(*mirek_to_xy(mirek))
    for point in action.get("gradient", {}).get("points", []):
        if xy := point.get("color", {}).get("xy"):
            return xy_to_hex(xy["x"], xy["y"])
    return ""


def _look_xy(look: dict) -> tuple[float, float]:
    """The colour a light shows for a look (gradients: their first point)."""
    if mirek := look.get("color_temperature", {}).get("mirek"):
        return mirek_to_xy(mirek)
    if "gradient" in look:
        look = look["gradient"]["points"][0]
    xy = look.get("color", {}).get("xy", {"x": 0.3127, "y": 0.329})
    return xy["x"], xy["y"]


class SceneController:
    def __init__(self, bridge: HueBridge, config_id: str = ""):
        self.bridge = bridge
        self.config_id = config_id
        self._lights: list[AreaLight] | None = None  # looked up once: costs 3 requests
        self.bridge_scenes: list[BridgeScene] = []
        self.active = False
        self.glow: list[tuple[float, tuple[float, float], float]] = []  # (x, xy, level) per light
        self._dynamic_stop = threading.Event()
        self._dynamic_thread: threading.Thread | None = None

    def set_config_id(self, config_id: str) -> None:
        self.config_id = config_id
        self._lights = None
        self.bridge_scenes = []

    def lights(self, refresh: bool = False) -> list[AreaLight]:
        if self._lights is None or refresh:
            self._lights = self.bridge.get_area_lights(self.config_id)
        return self._lights

    def scene(self, key: str, custom: list) -> Scene | BridgeScene | None:
        """The scene a settings key refers to (preset, custom or bridge scene)."""
        if key in SCENE_PRESETS:
            return SCENE_PRESETS[key]
        for entry in custom:
            if entry["id"] == key:
                return Scene(entry["name"], tuple(entry["colors"]))
        if key.startswith("bridge:"):
            if not any(s.id == key for s in self.bridge_scenes):
                self.list_bridge_scenes()
            return next((s for s in self.bridge_scenes if s.id == key), None)
        return None

    def apply(self, scene: Scene | BridgeScene, brightness: int, dynamic: bool) -> bool:
        """Show a scene; brightness (1-254) scales the scene's own levels."""
        self.stop_dynamic()
        lights = self.lights()
        if not lights:
            timed_print("Scenes: the entertainment area has no lights")
            return False
        fraction = brightness / 254
        looks = self._looks(scene, lights)
        if not self._send(lights, looks, fraction, 0, TRANSITION_MS):
            return False
        self.active = True
        if dynamic and len({str(look) for look in looks}) > 1:
            self._dynamic_stop = threading.Event()
            self._dynamic_thread = threading.Thread(
                target=self._dynamic_loop,
                args=(lights, looks, fraction, self._dynamic_stop),
                daemon=True,
                name="SceneDynamics",
            )
            self._dynamic_thread.start()
        return True

    def stop_dynamic(self) -> None:
        self._dynamic_stop.set()

    def deactivate(self) -> None:
        self.stop_dynamic()
        self.active = False
        self.glow = []

    def set_lights_on(self, on: bool) -> bool:
        self.deactivate()
        results = [self.bridge.set_light_state(light.id, {"on": {"on": on}}) for light in self.lights()]
        return any(results)

    def any_light_on(self) -> bool:
        return any(light.on for light in self.lights(refresh=True))

    def _looks(self, scene: Scene | BridgeScene, lights: list[AreaLight]) -> list[tuple[dict, float]]:
        """Per light, left to right: (colour part of the payload, brightness %)."""
        count = len(lights)
        if isinstance(scene, BridgeScene):
            fallback = scene.colors or ("#FFFFFF",)
            looks = []
            for i, light in enumerate(lights):
                action = scene.actions.get(light.id)
                if action:
                    look = {k: v for k, v in action.items() if k in _LOOK_KEYS}
                    pct = action.get("dimming", {}).get("brightness", 100.0)
                else:
                    look, pct = {"color": _xy(fallback[i % len(fallback)])}, 100.0
                looks.append((look, pct))
            return looks

        pct = scene.brightness / 254 * 100
        if scene.mirek:
            return [({"color_temperature": {"mirek": scene.mirek}}, pct)] * count
        colors = scene.colors
        looks = []
        for i, light in enumerate(lights):
            if light.gradient_points > 1 and len(colors) > 1:
                start = round(i * (len(colors) - 1) / max(1, count - 1))
                points = [
                    colors[(start + j) % len(colors)] for j in range(min(len(colors), light.gradient_points))
                ]
                look = {"gradient": {"points": [{"color": _xy(c)} for c in points]}}
            else:
                look = {"color": _xy(colors[round(i * (len(colors) - 1) / max(1, count - 1))])}
            looks.append((look, pct))
        return looks

    def _send(self, lights, looks, fraction: float, offset: int, transition_ms: int) -> bool:
        sent = False
        glow = []
        for i, light in enumerate(lights):
            look, pct = looks[(i + offset) % len(looks)]
            if "gradient" in look and light.gradient_points <= 1:
                look = {"color": look["gradient"]["points"][0]["color"]}
            payload = {
                "on": {"on": True},
                **look,
                "dimming": {"brightness": max(1.0, min(100.0, pct * fraction))},
                "dynamics": {"duration": transition_ms},
            }
            sent |= self.bridge.set_light_state(light.id, payload)
            glow.append((light.x, _look_xy(look), payload["dimming"]["brightness"] / 100))
        self.glow = glow
        return sent

    def _dynamic_loop(self, lights, looks, fraction: float, stop: threading.Event) -> None:
        offset = 0
        while not stop.wait(DYNAMIC_INTERVAL_S):
            offset += 1
            self._send(lights, looks, fraction, offset, int(DYNAMIC_INTERVAL_S * 1000))

    def list_bridge_scenes(self) -> list[BridgeScene]:
        """Scenes of rooms and zones that share lights with the area."""
        area = {light.id for light in self.lights()}
        if not area:
            return []
        device_lights = {
            device["id"]: {s["rid"] for s in device.get("services", []) if s.get("rtype") == "light"}
            for device in self.bridge.get_resources("device")
        }
        groups = {}
        for rtype in ("room", "zone"):
            for group in self.bridge.get_resources(rtype):
                members = set()
                for child in group.get("children", []):
                    if child.get("rtype") == "light":
                        members.add(child.get("rid"))
                    elif child.get("rtype") == "device":
                        members |= device_lights.get(child.get("rid"), set())
                groups[group["id"]] = (group.get("metadata", {}).get("name", ""), members)

        scenes = []
        for scene in self.bridge.get_resources("scene"):
            group_name, members = groups.get(scene.get("group", {}).get("rid"), ("", set()))
            actions = {
                a["target"]["rid"]: a.get("action", {})
                for a in scene.get("actions", [])
                if a.get("target", {}).get("rid") in area
            }
            if not (members & area) or not actions:
                continue
            colors = list(dict.fromkeys(filter(None, map(_action_hex, actions.values()))))
            scenes.append(
                BridgeScene(
                    id=f"bridge:{scene['id']}",
                    name=scene.get("metadata", {}).get("name", "Scene"),
                    group=group_name,
                    colors=tuple(colors[:5]) or ("#FFFFFF",),
                    actions=actions,
                )
            )
        self.bridge_scenes = sorted(scenes, key=lambda s: (s.group.lower(), s.name.lower()))
        return self.bridge_scenes
