"""Switching between Scenes, Games, Music and Video, live.

Games, music and video stream to the entertainment area over DTLS; scenes
are set over REST, which the bridge only follows once streaming has stopped.
Switching between video and games keeps the screen-share session running.
Calls block for up to a few seconds: make them from a worker thread.
"""

import time
from collections.abc import Callable
from enum import Enum
from typing import TYPE_CHECKING, Optional

from glowcast.utils.logging import timed_print

if TYPE_CHECKING:
    from glowcast.entertainment import EntertainmentStream
    from glowcast.music import MusicController
    from glowcast.scenes import SceneController
    from glowcast.sync import SyncController

SCENE_SETTLE_S = 1.0  # the bridge needs this after streaming stops before REST commands


class Mode(Enum):
    OFF = "off"
    SCENES = "scenes"
    GAMES = "games"
    MUSIC = "music"
    VIDEO = "video"


STREAMING_MODES = (Mode.GAMES, Mode.MUSIC, Mode.VIDEO)
SCREEN_MODES = (Mode.GAMES, Mode.VIDEO)


class ModeManager:
    def __init__(
        self,
        bridge,
        sync_controller: "SyncController",
        entertainment_stream: Optional["EntertainmentStream"],
        scenes: "SceneController",
        music_controller: Optional["MusicController"] = None,
    ):
        self.bridge = bridge
        self.sync_controller = sync_controller
        self.music_controller = music_controller
        self.entertainment_stream = entertainment_stream
        self.scenes = scenes
        self.current_mode = Mode.OFF
        self._on_mode_changed: Callable[[Mode], None] | None = None
        self.last_error = ""  # why the last switch failed
        self.in_use_by_other = False  # the last switch failed because another app streams
        self.take_over = False  # next switch stops another app's stream ("Start anyway")

    def set_entertainment_stream(self, stream, entertainment_config_id: str) -> None:
        """Use a new entertainment stream / area (after a settings change)."""
        self.entertainment_stream = stream
        self.scenes.set_config_id(entertainment_config_id)

    def set_mode_changed_callback(self, callback: Callable[[Mode], None]) -> None:
        self._on_mode_changed = callback

    def _set_mode(self, mode: Mode) -> None:
        self.current_mode = mode
        timed_print(f"ModeManager: Now in {mode.name} mode")
        if self._on_mode_changed:
            self._on_mode_changed(mode)

    def switch_to_screen(self, mode: Mode) -> bool:
        """Video or games sync."""
        timed_print(f"ModeManager: Switching to {mode.name} mode")
        self.last_error = ""
        if self.current_mode in SCREEN_MODES and self.sync_controller.is_running():
            self.sync_controller.mode = mode.value
            self._set_mode(mode)
            return True
        self.scenes.deactivate()
        self._stop_streaming()
        if not self._connect_stream():
            return False
        self.sync_controller.mode = mode.value
        self.sync_controller.start()
        self._set_mode(mode)
        return True

    def switch_to_music(self) -> bool:
        timed_print("ModeManager: Switching to MUSIC mode")
        self.last_error = ""
        if not self.music_controller:
            self.last_error = "Music mode is not available"
            return False
        self.scenes.deactivate()
        self._stop_streaming()
        if not self._connect_stream():
            return False
        if not self.music_controller.start():
            self.last_error = self.music_controller.last_error
            timed_print(f"ModeManager: Failed to start music mode: {self.last_error}")
            self.entertainment_stream.disconnect(self.bridge)
            return False
        self._set_mode(Mode.MUSIC)
        return True

    def switch_to_scene(
        self, scene, brightness: int, dynamic: bool, superseded: Callable[[], bool] = lambda: False
    ) -> bool:
        """Show a scene. When superseded() turns True, a newer switch wins."""
        timed_print(f"ModeManager: Switching to scene {scene.name}")
        self.last_error = ""
        self._stop_streaming()
        if self._disconnect_stream():
            for _ in range(10):
                if superseded():
                    return False
                time.sleep(SCENE_SETTLE_S / 10)
        if superseded():
            return False
        if not self.scenes.apply(scene, brightness, dynamic):
            self.last_error = "Could not set the scene"
            return False
        self._set_mode(Mode.SCENES)
        return True

    def turn_off(self, lights_off: bool = False) -> None:
        """Stop syncing; the bridge returns the lights to how they were."""
        timed_print("ModeManager: Turning OFF")
        self._stop_streaming()
        self._disconnect_stream()
        if lights_off:
            self.scenes.set_lights_on(False)
        else:
            self.scenes.deactivate()
        self._set_mode(Mode.OFF)

    def _stop_streaming(self) -> None:
        """Stop the video and music loops (the mode is off until a new one starts)."""
        self.current_mode = Mode.OFF
        if self.sync_controller.is_running():
            self.sync_controller.stop()
        if self.music_controller and self.music_controller.is_running():
            self.music_controller.stop()

    def _disconnect_stream(self) -> bool:
        stream = self.entertainment_stream
        # Also when openssl died on its own: the bridge still has the area streaming
        if stream and (stream.is_connected() or stream.area_started):
            stream.disconnect(self.bridge)
            return True
        return False

    def _connect_stream(self) -> bool:
        """Activate the area and connect DTLS, recording a reason on failure."""
        self.in_use_by_other = False
        stream = self.entertainment_stream
        if not stream:
            self.last_error = "No entertainment area selected in Settings"
            return False
        if stream.is_connected():
            return True
        take_over, self.take_over = self.take_over, False
        if stream.connect(self.bridge, take_over=take_over):
            return True
        self.in_use_by_other = stream.in_use_by_other
        self.last_error = stream.last_error or "Could not connect to the entertainment area"
        timed_print(f"ModeManager: Failed to connect: {self.last_error}")
        return False
