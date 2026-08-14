"""Video streaming routes for transcoded media playback."""

import logging
import os
import re
import time

import flask_babel
from flask import Response, make_response, request, send_file
from flask_smorest import Blueprint

_ = flask_babel.gettext

from pikaraoke.lib._debug_points import debug_point
from pikaraoke.lib.current_app import get_karaoke_instance
from pikaraoke.lib.file_resolver import FileResolver, get_tmp_dir

stream_bp = Blueprint("stream", __name__)


def _stale_url_short_circuit(k, id: str) -> bool:
    """Valida se id da stream NAO EH STALE (antigo de PID morto/reinicio).

    Retorna:
        True se ja respondemos 404 IMEDIATO (nao eh pra esperar manifesto).
        False se deve continuar (ffmpeg vivo ou id ainda pode aparecer).
    """
    try:
        pb = k.playback_controller
        if pb is None:
            return False
        now_url = pb.now_playing_url
        proc = pb.ffmpeg_process
        ffmpeg_alive = bool(proc and proc.poll() is None)

        belongs_current = False
        if now_url:
            # extrai id do arquivo da current now_playing_url
            base = now_url.rsplit("/", 1)[-1] if "/" in now_url else now_url
            cur_id_dot = base.split(".", 1)[0] if "." in base else base
            if cur_id_dot and cur_id_dot == id:
                belongs_current = True

        if belongs_current:
            # Eh a musica atual: normal, deve esperar FFmpeg (limitado em 1.5s abaixo)
            return False

        # NAO eh a musica atual. Arquivo ja existe? Se sim entao OK serve.
        test_path = os.path.join(get_tmp_dir(), f"{id}.m3u8")
        if os.path.isfile(test_path):
            return False
        test_path2 = os.path.join(get_tmp_dir(), f"{id}.mp4")
        if os.path.isfile(test_path2):
            return False

        if not ffmpeg_alive:
            logging.info(
                "[STREAM STALE 404 RAPIDO] id=%s nao pertence now_playing_url=%s, "
                "arquivo nao existe, ffmpeg morto. 404 imediato (antes 5s bloqueado!).",
                id, now_url,
            )
            return True
    except Exception as exc:
        logging.debug("_stale_url_short_circuit exc: %s", exc)
    return False


# Serves HLS playlist file - explicit .m3u8 extension
@stream_bp.route("/stream/<id>.m3u8")
def stream_playlist(id):
    """Serve HLS playlist file."""
    file_path = os.path.join(get_tmp_dir(), f"{id}.m3u8")
    k = get_karaoke_instance()
    t0 = time.time()

    # --- Collect pre-data for debug ---
    pb = k.playback_controller
    ffmpeg_proc = getattr(pb, "ffmpeg_process", None) if pb else None
    ffmpeg_alive_before = bool(ffmpeg_proc and ffmpeg_proc.poll() is None)
    now_url_before = getattr(pb, "now_playing_url", None) if pb else None
    belongs_current_before = False
    if now_url_before:
        base = now_url_before.rsplit("/", 1)[-1] if "/" in now_url_before else now_url_before
        cur = base.split(".", 1)[0] if "." in base else base
        belongs_current_before = bool(cur and cur == id)
    file_exists_before = bool(os.path.isfile(file_path))

    # [NOVO V7] STALE SHORT CIRCUIT: id nao pertence a musica atual, arquivo nao existe,
    # ffmpeg morto → 404 IMEDIATO (Nao aguarda 1.5s bloqueando thread!)
    stale_short_circuit_used = False
    if _stale_url_short_circuit(k, id):
        stale_short_circuit_used = True
        #region debug-point P3 short circuit 404
        try:
            debug_point(
                "P3_stream_playlist_404_SHORT_CIRCUIT",
                stream_id=id,
                wait_ms=round((time.time() - t0) * 1000, 1),
                stale_short_circuit_triggered=True,
                belongs_current_now_playing=belongs_current_before,
                ffmpeg_alive=ffmpeg_alive_before,
                manifest_exists_before=False,
                http_status=404,
            )
        except Exception:
            pass
        #endregion
        return Response("Stale playlist (old stream id from previous restart).", status=404)

    start_song_marker_called_type = "not_called"
    start_song_validate_status = None
    # Mark song as started when client connects (idempotent)
    # Validate stream ID matches current song to prevent stale requests from setting is_playing
    if not k.playback_controller.is_playing:
        now_playing_url = k.playback_controller.now_playing_url
        if now_playing_url and id in now_playing_url:
            # NOVO V7: valida ANTES que stream realmente existe e nao eh STALE
            status = k.playback_controller.validate_now_playing_alive()
            start_song_validate_status = status
            if status in ("ok", "waiting_ffmpeg_data"):
                k.playback_controller.start_song(stream_id_marker=f"hls-{id}")
                start_song_marker_called_type = "called_accepted"
            else:
                start_song_marker_called_type = "skipped_invalid_status"
                logging.info(
                    "[stream_playlist] Nao marcando start_song: status=%s id=%s url=%s",
                    status, id, now_playing_url,
                )

    # Wait for playlist file to exist (NOVO V7: 1.5s max, antes era 5s → trava thread)
    max_wait = 15  # 1.5 seconds total (15 * 100ms)
    wait_count = 0
    while not os.path.exists(file_path) and wait_count < max_wait:
        time.sleep(0.1)
        wait_count += 1

    wait_ms_total = round((time.time() - t0) * 1000, 1)
    file_exists_after_wait = bool(os.path.exists(file_path))
    http_status = 200 if file_exists_after_wait else 404
    #region debug-point P3 stream playlist request
    try:
        if http_status == 404:
            debug_point(
                "P3_stream_playlist_404_AFTER_TIMEOUT",
                stream_id=id,
                wait_ms=wait_ms_total,
                stale_short_circuit_triggered=stale_short_circuit_used,
                belongs_current_now_playing=belongs_current_before,
                ffmpeg_alive=ffmpeg_alive_before,
                manifest_exists_before=file_exists_before,
                manifest_exists_after_timeout=file_exists_after_wait,
                start_song_called=start_song_marker_called_type,
                start_song_validate_status=start_song_validate_status,
                http_status=404,
            )
        else:
            debug_point(
                "P3_stream_playlist_OK_200",
                stream_id=id,
                wait_ms=wait_ms_total,
                stale_short_circuit_triggered=stale_short_circuit_used,
                belongs_current_now_playing=belongs_current_before,
                ffmpeg_alive=ffmpeg_alive_before,
                manifest_exists_before=file_exists_before,
                manifest_exists_after_timeout=file_exists_after_wait,
                start_song_called=start_song_marker_called_type,
                start_song_validate_status=start_song_validate_status,
                http_status=200,
            )
    except Exception:
        pass
    #endregion

    if os.path.exists(file_path):
        # Read file content and return with no-cache headers
        # This is critical for iOS Safari which aggressively caches playlists
        with open(file_path, "r") as f:
            content = f.read()
        response = make_response(content)
        response.headers["Content-Type"] = "application/vnd.apple.mpegurl"
        response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
        return response
    else:
        return Response("Playlist not found", status=404)


# Serves HLS segment files - .m4s (fragmented MP4) extension
@stream_bp.route("/stream/<filename>.m4s")
def stream_segment_m4s(filename):
    """Serve HLS segment file (fragmented MP4)."""
    # Security: prevent directory traversal
    if ".." in filename or "/" in filename:
        return Response("Invalid segment", status=400)

    segment_path = os.path.join(get_tmp_dir(), f"{filename}.m4s")

    if os.path.exists(segment_path):
        return send_file(segment_path, mimetype="video/mp4")
    else:
        return Response(f"Segment not found: {filename}.m4s", status=404)


# Serves init.mp4 header file for fMP4 (with unique filenames per stream)
@stream_bp.route("/stream/<filename>_init.mp4")
def stream_init(filename):
    """Serve init.mp4 header file for fragmented MP4 streams."""
    # Security: prevent directory traversal
    if ".." in filename or "/" in filename:
        return Response("Invalid init file", status=400)

    init_path = os.path.join(get_tmp_dir(), f"{filename}_init.mp4")
    if os.path.exists(init_path):
        return send_file(init_path, mimetype="video/mp4")
    else:
        return Response("Init file not found", status=404)


# Legacy .ts support for backward compatibility
@stream_bp.route("/stream/<filename>.ts")
def stream_segment(filename):
    """Serve HLS segment file (MPEG-TS)."""
    # Security: prevent directory traversal
    if ".." in filename or "/" in filename:
        return Response("Invalid segment", status=400)

    segment_path = os.path.join(get_tmp_dir(), f"{filename}.ts")

    if os.path.exists(segment_path):
        return send_file(segment_path, mimetype="video/mp2t")
    else:
        return Response(f"Segment not found: {filename}.ts", status=404)


# Main streaming route - serves HLS or progressive MP4 based on file extension
@stream_bp.route("/stream/<id>")
def stream_main(id):
    """Route streaming request to HLS or progressive MP4."""
    # Check if it's an HLS request (.m3u8) or MP4 request (.mp4)
    if request.path.endswith(".m3u8"):
        return stream_playlist(id.replace(".m3u8", ""))
    elif request.path.endswith(".mp4"):
        return stream_progressive_mp4(id.replace(".mp4", ""))
    else:
        # Fallback: try HLS first
        return stream_playlist(id)


# Progressive MP4 streaming with init.mp4 + segments concatenation
# This method works with HLS-generated fMP4 segments but serves them as continuous MP4
# Compatible with Chrome, Firefox and RPi with hardware acceleration
@stream_bp.route("/stream/<id>.mp4")
def stream_progressive_mp4(id):
    """Stream progressive MP4 from HLS-generated segments."""
    file_path = os.path.join(get_tmp_dir(), f"{id}.mp4")
    k = get_karaoke_instance()

    # Mark song as started when client connects (idempotent)
    # Validate stream ID matches current song to prevent stale requests from setting is_playing
    if not k.playback_controller.is_playing:
        now_playing_url = k.playback_controller.now_playing_url
        if now_playing_url and id in now_playing_url:
            k.playback_controller.start_song(stream_id_marker=f"mp4-{id}")

    # Wait for output file to exist
    max_wait = 50  # 5 seconds max
    wait_count = 0
    while not os.path.exists(file_path) and wait_count < max_wait:
        time.sleep(0.1)
        wait_count += 1

    if not os.path.exists(file_path):
        return Response("Stream file not ready", status=404)

    def generate():
        position = 0  # Initialize the position variable
        chunk_size = 10240 * 1000 * 25  # Read file in up to 25MB chunks
        with open(file_path, "rb") as file:
            # Keep yielding file chunks as long as ffmpeg process is transcoding
            while k.playback_controller.ffmpeg_process.poll() is None:
                file.seek(position)  # Move to the last read position
                chunk = file.read(chunk_size)
                if chunk is not None and len(chunk) > 0:
                    yield chunk
                    position += len(chunk)  # Update the position with the size of the chunk
                time.sleep(1)  # Wait a bit before checking the file size again
            chunk = file.read(chunk_size)  # Read the last chunk
            yield chunk
            position += len(chunk)  # Update the position with the size of the chunk

    return Response(generate(), mimetype="video/mp4")


def stream_file_path_full(file_path):
    try:
        file_size = os.path.getsize(file_path)
        range_header = request.headers.get("Range", None)
        if not range_header:
            with open(file_path, "rb") as file:
                file_content = file.read()
            return Response(file_content, mimetype="video/mp4")
        # Extract range start and end from Range header (e.g., "bytes=0-499")
        range_match = re.search(r"bytes=(\d+)-(\d*)", range_header)
        start, end = range_match.groups()
        start = int(start)
        end = int(end) if end else file_size - 1
        # Generate response with part of file
        with open(file_path, "rb") as file:
            file.seek(start)
            data = file.read(end - start + 1)
        status_code = 206  # Partial content
        headers = {
            "Content-Type": "video/mp4",
            "Accept-Ranges": "bytes",
            "Content-Range": f"bytes {start}-{end}/{file_size}",
            "Content-Length": str(len(data)),
        }
        return Response(data, status=status_code, headers=headers)
    except IOError:
        return Response("File not found.", status=404)


# Streams the file in full with proper range headers
# (Safari compatible, but requires the ffmpeg transcoding to be complete to know file size)
@stream_bp.route("/stream/full/<id>")
def stream_full(id):
    """Stream video with range headers (Safari compatible)."""
    k = get_karaoke_instance()

    # Mark song as started when client connects (idempotent)
    # Validate stream ID matches current song to prevent stale requests from setting is_playing
    if not k.playback_controller.is_playing:
        now_playing_url = k.playback_controller.now_playing_url
        if now_playing_url and id in now_playing_url:
            k.playback_controller.start_song(stream_id_marker=f"full-{id}")

    file_path = os.path.join(get_tmp_dir(), f"{id}.mp4")
    return stream_file_path_full(file_path)


@stream_bp.route("/stream/bg_video")
def stream_bg_video():
    """Stream the background video file."""
    k = get_karaoke_instance()
    file_path = k.bg_video_path
    if k.bg_video_path is not None:
        return send_file(os.path.abspath(file_path), mimetype="video/mp4")
    else:
        return Response("Background video not found.", status=404)


# subtitle .ass
@stream_bp.route("/subtitle/<id>")
def stream_subtitle(id):
    """Serve subtitle file for the current song."""
    k = get_karaoke_instance()
    try:
        original_file_path = k.playback_controller.now_playing_filename
        now_playing_url = k.playback_controller.now_playing_url
        if original_file_path and now_playing_url and id in now_playing_url:
            fr = FileResolver(original_file_path)
            ass_file_path = fr.ass_file_path
            if ass_file_path and os.path.exists(ass_file_path):
                return send_file(
                    os.path.abspath(ass_file_path),
                    mimetype="text/plain",
                    as_attachment=False,
                    download_name=os.path.basename(ass_file_path),
                )
    except Exception as e:
        k.log_and_send(_("Failed to stream subtitle: ") + str(e), "danger")
        return Response("Subtitle streaming error.", status=500)
    return Response("Subtitle file not found for this stream ID.", status=404)
