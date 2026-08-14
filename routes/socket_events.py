"""Socket.IO event handlers for PiKaraoke."""

import logging
import os
import shutil
import subprocess
from collections.abc import Mapping
from typing import Any

from flask import request

from pikaraoke.lib.current_app import get_karaoke_instance

# Track connected splash screen clients and the elected master
splash_connections = set()
master_splash_id = None

# ---- helpers duracao real arquivo (V37: servidor NAO confia cegamente no front) ----
_ffprobe_path_cache: str | None = None
_duracao_cache: dict[str, tuple[float, float]] = {}  # path -> (duration_s, cache_at_unix)


def _find_ffprobe() -> str | None:
    """Encontra caminho do ffprobe (geralmente ao lado do ffmpeg)."""
    global _ffprobe_path_cache
    if _ffprobe_path_cache is not None:
        return _ffprobe_path_cache
    # 1) se temos ffmpeg binaria conhecida (via preferences depois), nao importa.
    #    Tentar nomes comuns no PATH primeiro.
    for name in ("ffprobe",):
        p = shutil.which(name)
        if p:
            _ffprobe_path_cache = p
            return p
    _ffprobe_path_cache = ""
    return None


def _ffprobe_duration_safe(file_path: str) -> float | None:
    """Retorna duracao REAL do arquivo fonte baixado em segundos.
    Usa ffprobe. Fallback: tenta aproximar por metadata de arquivo se falhar.
    """
    if not file_path or not os.path.isfile(file_path):
        return None
    # cache 1h por arquivo (musica baixada nao muda, evita 2x ffprobe por musica)
    try:
        st = os.stat(file_path)
    except OSError:
        return None
    key = f"{file_path}|{st.st_size}|{int(st.st_mtime)}"
    now = st.st_ctime or __import__("time").time()
    cached = _duracao_cache.get(key)
    if cached and (now - cached[1]) < 3600:
        return cached[0]
    bin_path = _find_ffprobe()
    if not bin_path:
        return None
    try:
        result = subprocess.run(
            [
                bin_path, "-v", "error",
                "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1",
                file_path,
            ],
            capture_output=True, text=True, timeout=15, check=False,
        )
        if result.returncode == 0:
            out = result.stdout.strip()
            if out:
                d = float(out)
                if d > 0:
                    _duracao_cache[key] = (d, now)
                    return d
    except Exception as exc:
        logging.debug("ffprobe duration falhou %s: %s", file_path, exc)
    return None


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
        position: float | None = None
        duration: float | None = None
        ffmpeg_running = False

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

        # =================================================================
        # V37: VALIDACAO FINAL SERVER-SIDE (NAO CONFIA CEGAMENTE NO FRONT)
        # Se arquivo fonte (baixado) EXISTE e front disse que acabou ("complete"),
        #   checar a DURACAO REAL DO ARQUIVO com ffprobe. Se a posicao atual
        #   (do payload ou do now_playing_position do playback) for MENOR que
        #   (duracao_real - 30s) → NAO PODE ACABOU! Front estava mentindo por
        #   causa de metadata HLS errada / duration NaN / Chrome bug.
        # =================================================================
        try:
            pc = k.playback_controller
            if position is None:
                np_pos = getattr(pc, "now_playing_position", None)
                try:
                    position = float(np_pos) if np_pos is not None else None
                except (TypeError, ValueError):
                    position = None
            source_fn = getattr(pc, "now_playing_filename", None)
            source_exists = bool(source_fn and os.path.isfile(str(source_fn)))
            if not ffmpeg_running:
                ffmpeg_running = bool(
                    getattr(pc, "ffmpeg_process", None) is not None
                    and getattr(pc.ffmpeg_process, "poll", lambda: None)() is None
                )

            reason_str = str(reason or "").strip().lower()
            front_says_complete = (
                reason_str == "complete"
                or reason_str == ""
                or reason_str == "unknown"
            )
            if source_exists and front_says_complete:
                dur_real = _ffprobe_duration_safe(str(source_fn))
                if dur_real is not None and dur_real > 0:
                    pos_comp = float(position) if position is not None else 0.0
                    # margem 30s: se o front disse que acabou e estamos a mais de
                    # 30s do final REAL do arquivo baixado → INCOMPLETO!
                    if pos_comp < (dur_real - 30.0):
                        if ffmpeg_running:
                            # FFmpeg vivo: musica NAO acabou. Pedir retry no
                            # front (continuar do ponto atual), NAO chamar end_song.
                            logging.warning(
                                "[V37 BOM: BLOQUEAMOS ENCERRAMENTO!] "
                                "front disse reason='complete' mas musica nao acabou! "
                                "FFmpeg ainda RODANDO. "
                                "pos=%.0fs duration_real_ffprobe=%.0fs falta=%.0fs. "
                                "file=%s. Pedindo retry no splash.",
                                pos_comp, dur_real, max(0.0, dur_real - pos_comp),
                                str(source_fn)[-80:],
                            )
                            socketio.emit(
                                "retry_current_song",
                                {
                                    "position": max(pos_comp - 1, 0),
                                    "reason": "server-ffprobe-ffmpeg-running",
                                },
                            )
                            return
                        # FFmpeg MORREU mas arquivo existe e posicao < final -30s.
                        # → musica parou na metade! Nao marcar complete, marcar
                        #   como erro.
                        new_reason = (
                            f"server-ffprobe-detected-incomplete "
                            f"({pos_comp:.0f}/{dur_real:.0f}s)"
                        )
                        logging.warning(
                            "[V37 BOM: TROCAMOS REASON!] front disse '%s' mas "
                            "musica incompleta (ffmpeg MORREU no meio). "
                            "Novo reason=%r | file=%s",
                            reason, new_reason, str(source_fn)[-80:],
                        )
                        reason = new_reason
        except Exception as exc_v37:
            logging.warning("[V37 ffprobe check falhou, continuando mesmo assim]: %s", exc_v37)

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
