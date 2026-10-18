"""Application wiring and shared services."""

import threading
from dataclasses import dataclass

from gi.repository import GLib

from glowcast.capture import ScreenCapture
from glowcast.colors import xy_to_rgb
from glowcast.config.settings_manager import SettingsManager
from glowcast.entertainment import EntertainmentStream
from glowcast.hue_bridge import HueBridge
from glowcast.mode_manager import STREAMING_MODES, Mode, ModeManager
from glowcast.music import MusicController
from glowcast.scenes import SceneController
from glowcast.sync import CAPTURE_FPS, SyncController
from glowcast.utils.logging import timed_print
from glowcast.zones import ZoneProcessor


@dataclass(frozen=True)
class BridgeStatus:
    connected: bool
    configured: bool
    bridge_ip: str
    entertainment_zone_name: str = ""
    entertainment_channel_count: int = 0
    entertainment_connected: bool = False


class AppContext:
    def __init__(self, settings: SettingsManager):
        self.settings = settings

        self.bridge = HueBridge(settings.hue.bridge_ip, settings.hue.app_key)
        self.capture = ScreenCapture(restore_token=settings.capture.restore_token, max_fps=CAPTURE_FPS)
        self.capture.set_on_restore_token_changed(self._on_restore_token_changed)

        self.entertainment_stream = None
        if settings.hue.client_key and settings.hue.entertainment_config_id:
            self.entertainment_stream = EntertainmentStream(
                bridge_ip=settings.hue.bridge_ip,
                app_key=settings.hue.app_key,
                client_key=settings.hue.client_key,
                entertainment_config_id=settings.hue.entertainment_config_id,
            )

        self.sync_controller = SyncController(
            bridge=self.bridge,
            capture=self.capture,
            zone_processor=ZoneProcessor(),
            settings=settings,
            entertainment_stream=self.entertainment_stream,
        )

        self.music_controller = MusicController(
            bridge=self.bridge,
            settings=settings.music,
            entertainment_stream=self.entertainment_stream,
            brightness=lambda: settings.lights.brightness,
        )

        self.scenes = SceneController(self.bridge, settings.hue.entertainment_config_id)
        self.mode_manager = ModeManager(
            bridge=self.bridge,
            sync_controller=self.sync_controller,
            entertainment_stream=self.entertainment_stream,
            scenes=self.scenes,
            music_controller=self.music_controller,
        )

        self._settings_lock = threading.Lock()

    def light_glow(self) -> list[tuple[float, float, tuple[float, float, float], float]]:
        """What each light shows right now, for the window glow: (x, z, rgb, level)."""
        mode = self.mode_manager.current_mode
        stream = self.entertainment_stream
        if mode in STREAMING_MODES and stream:
            positions = stream.get_channel_positions()
            return [
                (positions[c]["x"], positions[c]["z"], xy_to_rgb(*xy), brightness / 254)
                for c, (xy, brightness) in stream.last_colors.items()
                if c in positions
            ]
        if mode is Mode.SCENES:
            return [(x, 0.0, xy_to_rgb(*xy), level) for x, xy, level in self.scenes.glow]
        return []

    def stop_entertainment(self):
        """Disconnect from entertainment streaming zone."""
        if self.entertainment_stream and self.entertainment_stream.is_connected():
            self.entertainment_stream.disconnect(self.bridge)

    def shutdown(self) -> None:
        """Stop syncing; turn the lights off if the user asked for that."""
        lights_off = self.settings.ui.lights_off_on_close
        if lights_off or self.mode_manager.current_mode is not Mode.OFF:
            self.mode_manager.turn_off(lights_off=lights_off)

    def apply_settings(self) -> None:
        """Apply current settings to live components atomically."""
        with self._settings_lock:
            hue = self.settings.hue
            self.bridge.update_credentials(hue.bridge_ip, hue.app_key)
            self._apply_entertainment_settings()

    def _apply_entertainment_settings(self) -> None:
        """Rebuild the entertainment stream when the bridge, keys or zone change.

        The new stream must reach every holder: the mode manager is what
        connects it, the video and music controllers are what send on it.
        """
        hue = self.settings.hue
        wanted = (hue.bridge_ip, hue.app_key, hue.client_key, hue.entertainment_config_id)
        current = self.entertainment_stream
        if current is not None and wanted == (
            current.bridge_ip,
            current.app_key,
            current.client_key,
            current.entertainment_config_id,
        ):
            return
        configured = bool(hue.client_key and hue.entertainment_config_id)
        if current is None and not configured:
            return

        if self.mode_manager.current_mode in STREAMING_MODES:
            self.mode_manager.turn_off()
        self.stop_entertainment()

        stream = None
        if configured:
            stream = EntertainmentStream(
                bridge_ip=hue.bridge_ip,
                app_key=hue.app_key,
                client_key=hue.client_key,
                entertainment_config_id=hue.entertainment_config_id,
            )
        self.entertainment_stream = stream
        self.sync_controller.entertainment_stream = stream
        self.music_controller.entertainment_stream = stream
        self.mode_manager.set_entertainment_stream(stream, hue.entertainment_config_id)

    def _on_restore_token_changed(self, token: str) -> None:
        """Persist the portal restore token so screen-share consent survives
        app restarts. Called by ScreenCapture whenever the portal issues or
        refreshes a token.
        """
        self.settings.capture.restore_token = token
        try:
            self.settings.save()
            if token:
                timed_print(
                    f"[settings] Screen-share consent token saved to disk "
                    f"({len(token)} chars) — will persist across restarts"
                )
            else:
                timed_print("[settings] Screen-share consent token cleared from disk")
        except Exception as e:
            timed_print(f"Error persisting capture restore token: {e}")

    def get_bridge_status(self) -> BridgeStatus:
        """Whether the bridge answers, and the chosen entertainment area's name and size."""
        configured = bool(self.settings.hue.bridge_ip and self.settings.hue.app_key)
        connected = configured and self.bridge.test_connection()

        entertainment_zone_name = ""
        entertainment_channel_count = 0
        if connected and self.settings.hue.entertainment_config_id:
            configs = self.bridge.get_entertainment_configurations()
            for config in configs:
                if config.get("id") == self.settings.hue.entertainment_config_id:
                    entertainment_zone_name = config.get("name", "")
                    entertainment_channel_count = len(config.get("channels", []))
                    break

        entertainment_connected = (
            self.entertainment_stream is not None and self.entertainment_stream.is_connected()
        )

        return BridgeStatus(
            connected=connected,
            configured=configured,
            bridge_ip=self.settings.hue.bridge_ip,
            entertainment_zone_name=entertainment_zone_name,
            entertainment_channel_count=entertainment_channel_count,
            entertainment_connected=entertainment_connected,
        )

    def get_bridge_status_async(self, callback) -> None:
        """Fetch the bridge status in the background; callback(status) runs on the GTK thread."""

        def work():
            try:
                status = self.get_bridge_status()
            except Exception as e:
                timed_print(f"Error getting bridge status: {e}")
                status = BridgeStatus(
                    connected=False,
                    configured=bool(self.settings.hue.bridge_ip and self.settings.hue.app_key),
                    bridge_ip=self.settings.hue.bridge_ip,
                )
            GLib.idle_add(callback, status)

        threading.Thread(target=work, daemon=True).start()
