"""Settings: ignoring unknown keys, resetting, and dropping broken entries."""

import json

import pytest

from glowcast.config import settings_manager

CONFIG_ID = "25834667-7347-497d-ad1b-eaf83eecd7cb"


@pytest.fixture
def settings_in(tmp_path, monkeypatch):
    monkeypatch.setattr(settings_manager.SettingsManager, "_instance", None)
    monkeypatch.setattr(settings_manager.SettingsManager, "_get_config_dir", lambda self: tmp_path)
    return tmp_path


def test_unknown_settings_are_ignored(settings_in):
    (settings_in / "settings.json").write_text(
        json.dumps(
            {
                "hue": {"bridge_ip": "10.0.0.2", "app_key": "k", "entertainment_config_id": CONFIG_ID},
                "sync": {"intensity": "high", "removed_setting": True},
                "music": {"palette": "neon", "unknown": 1},
                "removed_section": {"brightness": 30},
            }
        )
    )
    settings = settings_manager.SettingsManager()
    assert settings.hue.bridge_ip == "10.0.0.2"
    assert settings.hue.entertainment_config_id == CONFIG_ID
    assert settings.sync.intensity == "high"
    assert settings.music.palette == "neon"
    assert settings.games.intensity == "moderate"


def test_reset_keeps_pairing_and_updates_live_objects(settings_in):
    settings = settings_manager.SettingsManager()
    settings.hue.bridge_ip = "10.0.0.2"
    settings.capture.restore_token = "token"
    settings.capture.capture_area = [0.1, 0.1, 0.9, 0.9]
    music = settings.music  # what the music loop holds on to
    music.palette = "neon"
    settings.lights.brightness = 0.3

    settings.reset_preferences()
    assert music.palette == "ember"  # same object, default value
    assert settings.lights.brightness == 1.0
    assert settings.capture.capture_area == [0.0, 0.0, 1.0, 1.0]
    assert settings.hue.bridge_ip == "10.0.0.2"
    assert settings.capture.restore_token == "token"


def test_broken_custom_palettes_are_dropped(settings_in):
    settings = settings_manager.SettingsManager()
    settings.music.custom_palettes = [
        {"id": "custom-1", "name": "Mine", "colors": ["#FF0000", "nope", "#00FF00"]},
        {"name": "no id", "colors": ["#FF0000"]},
    ]
    settings.music.palette = "custom-1"
    settings.save()
    assert settings.music.custom_palettes == [
        {"id": "custom-1", "name": "Mine", "colors": ["#FF0000", "#00FF00"]}
    ]
    assert settings.music.palette == "custom-1"
