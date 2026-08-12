"""Socket.IO event handlers for PiKaraoke."""

import logging
from collections.abc import Mapping
from typing import Any

from flask import request

from pikaraoke.lib.current_app import get_karaoke_instance

# Track connected splash screen clients and the elected master
splash_connections = set()
master_splash_id = None


def setup_socket_events(socketio):
    """Register Socket.IO event handlers.

    Args:
        socketio: The SocketIO instance.
    """

    def _parse_float(value: Any) -> float | None:
        try:
            return float(value) if value is not None else None
        except (TypeError, ValueError):
            return None

    @socketio.on("end_song")
    def end_song(payload: str | Mapping | None) -> None:
        """Handle end_song WebSocket event from client.

        Accepts either a legacy string reason or a telemetry payload from
        the splash screen so premature endings can be distinguished from a
        genuine end-of-song event.
        """
        k = get_karaoke_instance()

        reason = None
        if isinstance(payload, Mapping):
            reason = payload.get("reason")
            position = _parse_float(payload.get("position"))
            duration = _parse_float(payload.get("duration"))
            ended_early = bool(duration and position is not None and position < (duration - 3))
            ffmpeg_running = (
                k.playback_controller.ffmpeg_process is not None
                and k.playback_controller.ffmpeg_process.poll() is None
            )

            if ended_early:
                is_startup_failure = (position is not None and position < 5.0) or (
                    reason == "failed to start"
                )
                if ffmpeg_running or is_startup_failure:
                    msg = (
                        "Splash client ended early (startup failure, will retry). "
                        if is_startup_failure
                        else "Splash client ended early but FFmpeg is still running. "
                    )
                    logging.warning(
                        msg +
                        f"Requesting client reload: position={position:.2f}s "
                        f"duration={duration:.2f}s payload={dict(payload)}"
                    )
                    socketio.emit(
                        "retry_current_song",
                        {"position": max(position - 1, 0), "reason": reason},
                    )
                    return

                reason = f"client-ended-early ({position:.1f}/{duration:.1f}s)"
                logging.warning(
                    "Splash client reported completion before track duration and FFmpeg "
                    f"was no longer running: payload={dict(payload)}"
                )
            elif reason:
                logging.info(f"Splash client requested end_song: {dict(payload)}")
        else:
            reason = payload

        k.playback_controller.end_song(reason)

    @socketio.on("start_song")
    def start_song() -> None:
        """Handle start_song WebSocket event when playback begins."""
        k = get_karaoke_instance()
        k.playback_controller.start_song(stream_id_marker=f"ws-{request.sid}")

    @socketio.on("clear_notification")
    def clear_notification() -> None:
        """Handle clear_notification WebSocket event to dismiss notifications."""
        k = get_karaoke_instance()
        k.reset_now_playing_notification()

    @socketio.on("register_splash")
    def register_splash() -> None:
        """Handle splash screen registration and assign master/slave roles."""
        global master_splash_id
        sid = request.sid
        splash_connections.add(sid)
        logging.info(f"Splash screen registered: {sid}")

        if master_splash_id is None:
            master_splash_id = sid
            socketio.emit("splash_role", "master", room=sid)
            logging.info(f"Master splash screens assigned: {sid}")
        else:
            socketio.emit("splash_role", "slave", room=sid)
            logging.info(f"Slave splash screens assigned: {sid}")

    @socketio.on("playback_position")
    def handle_playback_position(position: float) -> None:
        """Handle playback_position WebSocket event from the master splash screen.

        Args:
            position: Current playback position in seconds.
        """
        global master_splash_id
        sid = request.sid
        if sid == master_splash_id:
            k = get_karaoke_instance()
            k.playback_controller.now_playing_position = position
            # Broadcast position to all other splash screens (slaves)
            socketio.emit("playback_position", position, include_self=False)

    @socketio.on("disconnect")
    def handle_disconnect() -> None:
        """Handle Socket.IO client disconnection and manage splash role handover."""
        global master_splash_id
        sid = request.sid
        if sid in splash_connections:
            splash_connections.remove(sid)
            logging.info(f"Splash screen disconnected: {sid}")
            if sid == master_splash_id:
                master_splash_id = None
                logging.info("Master splash disconnected, electing new master")
                if splash_connections:
                    # Elect new master from remaining connections
                    new_master = next(iter(splash_connections))
                    master_splash_id = new_master
                    socketio.emit("splash_role", "master", room=new_master)
                    logging.info(f"New master splash elected: {new_master}")

    @socketio.on("request_mic_devices")
    def handle_request_mic_devices() -> None:
        """Client requests the current mic device list from the server."""
        k = get_karaoke_instance()
        socketio.emit(
            "mic_devices_state",
            k.sound_manager.get_enriched_devices(),
            room=request.sid,
        )

    @socketio.on("request_mic_settings")
    def handle_request_mic_settings() -> None:
        """Client requests current mic global settings (latency, echo cancel)."""
        k = get_karaoke_instance()
        socketio.emit(
            "mic_settings_state",
            k.sound_manager.get_mic_settings_state(),
            room=request.sid,
        )

    @socketio.on("mic_latency_change")
    def handle_mic_latency_change(data: dict) -> None:
        """Handle mic latency change from control UI."""
        k = get_karaoke_instance()
        latency_ms = int(data.get("latency_ms", 50))
        state = k.sound_manager.set_latency_ms(latency_ms)
        socketio.emit("mic_settings_state", state)

    @socketio.on("mic_echo_cancel_change")
    def handle_mic_echo_cancel_change(data: dict) -> None:
        """Handle echo cancellation toggle from control UI."""
        k = get_karaoke_instance()
        enabled = bool(data.get("enabled", False))
        state = k.sound_manager.set_echo_cancel(enabled)
        socketio.emit("mic_settings_state", state)

    @socketio.on("mic_refresh")
    def handle_mic_refresh() -> None:
        """Re-enumerate mic and output devices server-side and broadcast updated lists."""
        k = get_karaoke_instance()
        enriched = k.sound_manager.refresh()
        socketio.emit("mic_devices_state", enriched)

    @socketio.on("mic_update")
    def handle_mic_update(data: dict) -> None:
        """Handle mic configuration change from control UI.

        Persists settings and activates/deactivates mic server-side.
        """
        k = get_karaoke_instance()
        label = data.get("label", "")
        device_id = str(data.get("deviceId", ""))
        enabled = data.get("enabled", False)
        volume = data.get("volume", 1.0)

        if label:
            settings = k.sound_manager.load_settings()
            new_state = {"enabled": enabled, "volume": volume}
            if settings.get(label) == new_state:
                return
            settings[label] = new_state
            k.sound_manager.save_settings(settings)

        # Activate or deactivate the mic stream server-side
        if enabled:
            k.sound_manager.activate(device_id, volume)
        else:
            k.sound_manager.deactivate(device_id)

        logging.info(f"Mic update: {label} enabled={enabled} volume={volume}")
        socketio.emit("mic_update", data)
