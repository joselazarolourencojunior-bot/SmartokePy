"""FFmpeg utilities for media processing and transcoding."""

from __future__ import annotations

import logging
import platform
import subprocess
from typing import TYPE_CHECKING, Any

import ffmpeg

from pikaraoke.lib.get_platform import is_running_in_docker

if TYPE_CHECKING:
    from pikaraoke.lib.file_resolver import FileResolver


def get_media_duration(file_path: str) -> int | None:
    """Get the duration of a media file in seconds.

    Args:
        file_path: Path to the media file.

    Returns:
        Duration in seconds (rounded), or None if unable to determine.
    """
    try:
        duration = ffmpeg.probe(file_path)["format"]["duration"]
        return round(float(duration))
    except:
        return None


def validate_media_file(file_path: str) -> tuple[bool, str | None]:
    """Perform a lightweight integrity check on a media file.

    Returns:
        (True, None) when the file looks usable, otherwise (False, reason).
    """
    try:
        probe = ffmpeg.probe(file_path)
    except Exception as e:
        return False, f"ffprobe failed: {e}"

    format_info = probe.get("format", {})
    streams = probe.get("streams", [])

    if not streams:
        return False, "file has no media streams"

    size = format_info.get("size")
    try:
        if size is not None and int(size) <= 0:
            return False, "file size is zero"
    except (TypeError, ValueError):
        pass

    duration = format_info.get("duration")
    try:
        if duration is not None and float(duration) <= 0:
            return False, "media duration is zero"
    except (TypeError, ValueError):
        pass

    has_audio = any(stream.get("codec_type") == "audio" for stream in streams)
    has_video = any(stream.get("codec_type") == "video" for stream in streams)
    if not has_audio and not has_video:
        return False, "file has neither audio nor video stream"

    return True, None


def build_ffmpeg_cmd(
    fr: FileResolver,
    semitones: int = 0,
    normalize_audio: bool = True,
    force_mp4_encoding: bool = False,
    buffer_fully_before_playback: bool = False,
    avsync: float = 0,
    cdg_pixel_scaling: bool = False,
) -> Any:
    """Build an ffmpeg command for transcoding media.

    Handles video/audio codec selection, pitch shifting, audio normalization,
    and CDG file rendering.

    Args:
        fr: FileResolver instance with source file information.
        semitones: Number of semitones to shift pitch (0 = no shift).
        normalize_audio: Whether to apply loudness normalization.
        force_mp4_encoding: If True, force mp4 encoding.
        avsync: Audio/video sync adjustment in seconds.
        cdg_pixel_scaling: Enable pixel scaling for CDG rendering.

    Returns:
        ffmpeg stream object ready to execute with run_async().
    """
    avsync = float(avsync)
    is_cdg = fr.cdg_file_path is not None
    is_transposed = semitones != 0

    if fr.file_path is None:
        raise ValueError("File path is required to build ffmpeg command")

    # Use h/w acceleration:
    #   Raspberry Pi ARM -> h264_v4l2m2m (so como antes)
    #   Intel x86/x64 (Dell OptiPlex 7010 i3/i5) -> tenta VA-API h264_vaapi se disponivel,
    #   senao software libx264 MAS com downscale 854x480 + bitrate reduzido (2.5M) + preset
    #   ultrafast para o Dell antigo nao travar no meio da musica por gargalo de CPU.
    using_hardware_encoder = supports_hardware_h264_encoding()
    is_x86_intel = not using_hardware_encoder and _is_intel_x86_with_vaapi()
    if using_hardware_encoder:
        default_vcodec = "h264_v4l2m2m"
        default_vbitrate = "2M"
    elif is_x86_intel:
        # tenta usar encoder VA-API Intel (Core i3/i5 IvyBridge / SandyBridge 2a/3a geracao tem Quick Sync Video)
        default_vcodec = "h264_vaapi" if _has_encoder("h264_vaapi") else "libx264"
        default_vbitrate = "2500k" if default_vcodec == "h264_vaapi" else "2000k"
    else:
        default_vcodec = "libx264"
        default_vbitrate = "3M"

    # CDG sempre precisa encoding; MP4 pode copiar stream (ja H.264) a menos que precise
    # fazer downscale ou outra operacao.
    if is_cdg:
        vcodec = default_vcodec if default_vcodec == "libx264" else "libx264"
    else:
        # OTIMIZACAO DELL ANTIGO: para arquivos .mp4 com codec h264 E sem transpose/normalizacao
        # COPIA stream de video e audio, ganhando velocidade extrema (0 transcode).
        vcodec = "copy" if fr.file_extension == ".mp4" else default_vcodec

    # CDG 500k, hwenc 2M, x86 dell 2.5M vaapi / 2M sw enc, outros 3M
    if is_cdg:
        vbitrate = "500k"
    elif using_hardware_encoder:
        vbitrate = "2M"
    else:
        vbitrate = default_vbitrate

    # Audio: para evitar trabalho desnecessario no Dell, se arquivo for mp4 sem nenhum
    # processamento de audio, COPIA audio tambem (copy). So re-encode AAC se cdg, transpose,
    # normalize, avsync.
    acodec = (
        "aac"
        if is_cdg or is_transposed or normalize_audio or avsync != 0
        else ("copy" if fr.file_extension == ".mp4" else "aac")
    )

    # OTIMIZACAO DELL: filtro scale para 854x480 (wide 480p) quando for encoder software
    # e arquivo nao for CDG. CPU i3/i5 antiga Nao aguenta 1080p em tempo real sem travar.
    need_software_downscale = (
        not is_cdg
        and not using_hardware_encoder
        and vcodec == "libx264"
    )
    need_vaapi_downscale = (
        not is_cdg and default_vcodec == "h264_vaapi" and vcodec == "h264_vaapi"
    )

    if fr.file_extension in [".webm", ".avi", ".mov", ".mkv"]:
        input = ffmpeg.input(fr.file_path, **{"fflags": "+genpts"})
    else:
        input = ffmpeg.input(fr.file_path)
    audio = input.audio

    if avsync > 0:
        audio = audio.filter("adelay", f"{avsync * 1000}|{avsync * 1000}")
    elif avsync < 0:
        audio = audio.filter("atrim", start=-avsync)

    if is_transposed:
        audio = audio.filter("rubberband", pitch=2 ** (semitones / 12))

    if normalize_audio:
        audio = audio.filter("loudnorm", i=-16, tp=-1.5, lra=11)

    if is_cdg:
        logging.info("Playing CDG/MP3 file: " + fr.file_path)
        cdg_input = ffmpeg.input(fr.cdg_file_path, copyts=None)
        video = cdg_input.video.filter("fps", fps=25)
        if cdg_pixel_scaling:
            video = video.filter("scale", -1, 720, flags="neighbor")
    else:
        video = input.video
        if need_software_downscale:
            # scale 854x480 wide, mantem aspect ratio (filtro lanczos nao, bilinear rapido)
            video = video.filter("scale", "min(854,iw)", "min(480,ih)", flags="bilinear")
            video = video.filter("setsar", "1/1")

    # Build output based on format
    if force_mp4_encoding:
        movflags = (
            "+faststart" if buffer_fully_before_playback else "frag_keyframe+default_base_moof"
        )
        output = ffmpeg.output(
            audio,
            video,
            fr.output_file,
            vcodec=vcodec,
            acodec=acodec,
            preset="ultrafast",
            listen=1,
            f="mp4",
            video_bitrate=vbitrate,
            movflags=movflags,
            **({"pix_fmt": "yuv420p"} if is_cdg else {}),
        )
    else:
        # HLS format with fMP4 segments
        # Both MP4 and HLS streaming modes use this - difference is in serving:
        # - mp4: Stream concatenates init + segments for progressive playback
        # - hls: Browser requests segments via .m3u8 playlist
        output = ffmpeg.output(
            audio,
            video,
            fr.output_file,
            vcodec=vcodec,
            acodec="aac",
            audio_bitrate="192k",
            ac=2,  # Force stereo
            ar=48000,  # Standard sample rate
            preset="ultrafast",
            f="hls",
            hls_time=3,
            hls_list_size=0,
            hls_playlist_type="event",
            hls_segment_type="fmp4",
            hls_fmp4_init_filename=fr.init_filename,
            hls_segment_filename=fr.segment_pattern,
            video_bitrate=vbitrate,
            # CDG needs pix_fmt for proper color space
            **({"pix_fmt": "yuv420p"} if is_cdg else {}),
            **{
                "vsync": "cfr",
                "avoid_negative_ts": "make_zero",
            },
        )

    args = output.get_args()
    logging.debug(f"COMMAND: ffmpeg " + " ".join(args))
    return output


def get_ffmpeg_version() -> str:
    """Get the installed FFmpeg version string.

    Returns:
        Version string, or an error message if FFmpeg is not installed
        or version cannot be parsed.
    """
    try:
        # Execute the command 'ffmpeg -version'
        result = subprocess.run(
            ["ffmpeg", "-version"], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True
        )
        # Parse the first line to get the version
        first_line = result.stdout.split("\n")[0]
        version_info = first_line.split(" ")[2]  # Assumes the version info is the third element
        return version_info
    except FileNotFoundError:
        return "FFmpeg is not installed"
    except IndexError:
        return "Unable to parse FFmpeg version"


def is_transpose_enabled() -> bool:
    """Check if FFmpeg has the rubberband filter for pitch shifting.

    Returns:
        True if rubberband filter is available, False otherwise.
    """
    try:
        filters = subprocess.run(["ffmpeg", "-filters"], capture_output=True)
    except FileNotFoundError:
        return False
    except IndexError:
        return False
    return "rubberband" in filters.stdout.decode()


def supports_hardware_h264_encoding() -> bool:
    """Check if hardware H.264 encoding (h264_v4l2m2m) is available.

    Only returns True on ARM architecture (Raspberry Pi) where h264_v4l2m2m
    is actually supported. On x86/Intel systems, returns False to use software encoding.

    Returns:
        True if hardware encoding is available, False otherwise.
    """
    # Check CPU architecture first - h264_v4l2m2m only works on ARM
    arch = platform.machine().lower()
    is_arm = any(arm_variant in arch for arm_variant in ["arm", "aarch"])

    if not is_arm:
        # Not ARM (probably Intel x86/x64), don't use h264_v4l2m2m
        logging.debug(f"CPU architecture {arch} is not ARM, using software encoder")
        return False

    if is_running_in_docker():
        # Docker containers do not have access to the GPU
        logging.debug("Running in Docker where GPU access is not available, using software encoder")
        return False

    # On ARM, check if h264_v4l2m2m is available
    try:
        codecs = subprocess.run(["ffmpeg", "-codecs"], capture_output=True)
    except FileNotFoundError:
        return False
    except IndexError:
        return False

    has_encoder = "h264_v4l2m2m" in codecs.stdout.decode()
    if has_encoder:
        logging.info("ARM platform detected, using h264_v4l2m2m hardware encoder")
    else:
        logging.debug("ARM platform but h264_v4l2m2m not available")

    return has_encoder


_HAS_ENCODER_CACHE: dict[str, bool] = {}


def _has_encoder(name: str) -> bool:
    """Verifica se ffmpeg tem o encoder name disponivel. Cache em memoria."""
    if name in _HAS_ENCODER_CACHE:
        return _HAS_ENCODER_CACHE[name]
    try:
        encoders_raw = subprocess.run(
            ["ffmpeg", "-encoders"], capture_output=True, text=True, timeout=15
        )
    except Exception:
        _HAS_ENCODER_CACHE[name] = False
        return False
    ok = f" {name} " in (" " + encoders_raw.stdout.replace("\n", " \n") + " ") or name in encoders_raw.stdout
    logging.info(f"FFmpeg encoder '{name}' available: {ok}")
    _HAS_ENCODER_CACHE[name] = ok
    return ok


def _is_intel_x86_with_vaapi() -> bool:
    """Retorna True se for x86/x64 Intel (Dell OptiPlex) e VA-API possivelmente disponivel.
    Nao falha: apenas heuristic. Detalhes decididos por _has_encoder em build_ffmpeg_cmd.
    """
    arch = platform.machine().lower()
    is_x86 = any(v in arch for v in ["x86", "amd64", "i386", "i686"])
    if not is_x86:
        return False
    try:
        cpu = subprocess.run(
            ["cat", "/proc/cpuinfo"], capture_output=True, text=True, timeout=5
        ).stdout.lower()
    except Exception:
        # Se /proc nao existir (Win/Mac local), assume True para x86 para que o _has_encoder decida.
        return True
    return any(tok in cpu for tok in ["intel", "core i3", "core i5", "core i7", "pentium", "celeron"])


def is_ffmpeg_installed() -> bool:
    """Check if FFmpeg is installed and accessible.

    Returns:
        True if FFmpeg is installed, False otherwise.
    """
    try:
        subprocess.run(["ffmpeg", "-version"], capture_output=True)
    except FileNotFoundError:
        return False
    return True
