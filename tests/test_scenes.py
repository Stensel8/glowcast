"""Scenes: colours spread over the area's lights, bridge scenes stay inside the area."""

from glowcast.hue_bridge import AreaLight
from glowcast.presets import SCENE_PRESETS, Scene
from glowcast.scenes import SceneController


class FakeBridge:
    def __init__(self, lights, resources=None):
        self.lights, self.resources, self.sent = lights, resources or {}, []

    def get_area_lights(self, config_id):
        return self.lights

    def get_resources(self, rtype):
        return self.resources.get(rtype, [])

    def set_light_state(self, light_id, payload):
        self.sent.append((light_id, payload))
        return True


def lights(count, gradient_at=None):
    return [
        AreaLight(f"l{i}", f"Light {i}", -1 + i * 0.5, 5 if i == gradient_at else 0, False)
        for i in range(count)
    ]


def test_palette_spreads_from_left_to_right_and_gradients_get_all_colours():
    bridge = FakeBridge(lights(3, gradient_at=1))
    scenes = SceneController(bridge, "area")
    assert scenes.apply(Scene("Test", ("#FF0000", "#00FF00", "#0000FF"), 254), 127, dynamic=False)
    left, middle, right = (payload for _id, payload in bridge.sent)
    assert left["color"]["xy"]["x"] > 0.6  # red
    assert right["color"]["xy"]["y"] < 0.1  # blue
    assert len(middle["gradient"]["points"]) == 3
    assert left["dimming"]["brightness"] == 50.0  # half of the scene's full brightness


def test_white_scenes_use_colour_temperature():
    bridge = FakeBridge(lights(2))
    SceneController(bridge, "area").apply(SCENE_PRESETS["relax"], 254, dynamic=False)
    assert all(p["color_temperature"]["mirek"] == 447 for _id, p in bridge.sent)


def test_bridge_scenes_only_touch_the_area_lights():
    resources = {
        "device": [{"id": "d0", "services": [{"rid": "l0", "rtype": "light"}]}],
        "room": [
            {"id": "room", "metadata": {"name": "Living"}, "children": [{"rid": "d0", "rtype": "device"}]}
        ],
        "zone": [],
        "scene": [
            {
                "id": "s1",
                "metadata": {"name": "Sunset"},
                "group": {"rid": "room", "rtype": "room"},
                "actions": [
                    {
                        "target": {"rid": "l0", "rtype": "light"},
                        "action": {"color": {"xy": {"x": 0.6, "y": 0.35}}, "dimming": {"brightness": 80.0}},
                    },
                    {"target": {"rid": "outside", "rtype": "light"}, "action": {"on": {"on": True}}},
                ],
            },
            {"id": "s2", "metadata": {"name": "Elsewhere"}, "group": {"rid": "other"}, "actions": []},
        ],
    }
    bridge = FakeBridge(lights(2), resources)
    scenes = SceneController(bridge, "area")
    found = scenes.list_bridge_scenes()
    assert [(s.name, s.group) for s in found] == [("Sunset", "Living")]
    assert scenes.scene("bridge:s1", []) is found[0]

    scenes.apply(found[0], 254, dynamic=False)
    assert [light for light, _payload in bridge.sent] == ["l0", "l1"]  # never "outside"
    assert bridge.sent[0][1]["dimming"]["brightness"] == 80.0


def test_scenes_report_what_each_light_shows_for_the_window_glow():
    bridge = FakeBridge(lights(2, gradient_at=1))
    scenes = SceneController(bridge, "area")
    scenes.apply(Scene("Test", ("#FF0000", "#0000FF"), 254), 254, dynamic=False)
    (x0, xy0, level0), (x1, xy1, level1) = scenes.glow
    assert x0 < x1 and level0 == level1 == 1.0
    assert xy0[0] > 0.6 and xy1[1] < 0.1  # red on the left, blue (first gradient point) on the right
    scenes.deactivate()
    assert scenes.glow == []
