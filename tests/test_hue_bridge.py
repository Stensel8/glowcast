"""Hue bridge: reading entertainment areas, quietly or with the reason it failed."""

import pytest

from glowcast.hue_bridge import BridgeError, HueBridge


def test_unreadable_areas_are_empty_unless_errors_are_asked_for():
    bridge = HueBridge("192.0.2.1", "")  # no app key yet, as before pairing
    assert bridge.get_entertainment_configurations() == []
    with pytest.raises(BridgeError, match="not set up"):
        bridge.get_entertainment_configurations(raise_errors=True)
