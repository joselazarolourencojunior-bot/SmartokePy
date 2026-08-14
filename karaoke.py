"""Core karaoke engine for managing songs, queue, and playback."""

import logging
import os
import socket
import subprocess
import threading
import time
from typing import Any

import qrcode
from flask_babel import _
from qrcode.image.pure import PyPNGImage

from pikaraoke.lib.download_manager import DownloadManager
from pikaraoke.lib.events import EventSystem
from pikaraoke.lib.ffmpeg import (
    get_ffmpeg_version,
    is_transpose_enabled,
    supports_hardware_h264_encoding,
)
from pikaraoke.lib.get_platform import (
    get_data_directory,
    get_os_version,
    get_platform,
    is_raspberry_pi,
)
from pikaraoke.lib.karaoke_database import KaraokeDatabase
from pikaraoke.lib.library_scanner import LibraryScanner, ScanResult
from pikaraoke.lib.network import get_ip
from pikaraoke.lib.playback_controller import PlaybackController
from pikaraoke.lib.preference_manager import PreferenceManager
from pikaraoke.lib.queue_manager import QueueManager
from pikaraoke.lib.singer_manager import SingerManager
from pikaraoke.lib.song_manager import SongManager
from pikaraoke.lib.sound_manager import SoundManager
from pikaraoke.lib.youtube_dl import (
    get_search_results,
    get_youtubedl_version,
    upgrade_youtubedl,
)
from pikaraoke.version import __version__ as VERSION


class Karaoke:
    """Main karaoke engine managing songs, queue, and playback.

    This class handles all core karaoke functionality including:
    - Song queue management
    - YouTube video downloading
    - Playback coordination via PlaybackController
    - User preferences
    - QR code generation

    Attributes:
        available_songs: List of available song file paths.
        queue_manager: Queue management for songs.
        playback_controller: Playback state and stream coordination.
        volume: Current volume level (0.0 to 1.0).
    """

    song_manager: SongManager
    queue_manager: QueueManager
    playback_controller: PlaybackController
    singer_manager: SingerManager

    now_playing_notification: str | None = None
    volume: float
    # Throttle para nao spammar tela de notificacao da "Vez Sagrada bloqueada"
    # (maximo uma notificacao por 10s quando o cantor da vez nao tem musica)
    _vez_sagrada_last_block_notif: float = 0.0

    qr_code_path: str | None = None
    base_path: str = os.path.dirname(__file__)
    loop_interval: int = 500  # in milliseconds
    default_logo_path: str = os.path.join(base_path, "static", "images", "logo.png")
    default_bg_music_path: str = os.path.join(base_path, "static", "music")
    default_bg_video_path: str = os.path.join(base_path, "static", "video", "night_sea.mp4")
    screensaver_timeout: int

    normalize_audio: bool
    show_splash_clock: bool

    # Download manager for serialized downloads
    download_manager: DownloadManager

    # Microphone manager for server-side mic passthrough
    sound_manager: SoundManager

    # Event system and preferences
    events: EventSystem
    preferences: PreferenceManager

    def __init__(
        self,
        # Non-preference parameters (keep their own defaults)
        additional_ytdl_args: str | None = None,
        bg_music_path: str | None = None,
        bg_video_path: str | None = None,
        config_file_path: str = "config.ini",
        download_path: str = "/usr/lib/pikaraoke/songs",
        hide_splash_screen: bool | None = None,
        log_level: int = logging.DEBUG,
        logo_path: str | None = None,
        port: int = 5555,
        prefer_hostname: bool | None = None,
        preferred_language: str | None = None,
        socketio=None,
        streaming_format: str = "hls",
        url: str | None = None,
        youtubedl_proxy: str | None = None,
        # Preference parameters (defaults from PreferenceManager.DEFAULTS)
        avsync: float | None = None,
        bg_music_volume: float | None = None,
        browse_results_per_page: int | None = None,
        buffer_size: int | None = None,
        cdg_pixel_scaling: bool | None = None,
        complete_transcode_before_play: bool | None = None,
        disable_bg_music: bool | None = None,
        disable_bg_video: bool | None = None,
        disable_score: bool | None = None,
        enable_mic_passthrough: bool | None = None,
        hide_notifications: bool | None = None,
        hide_overlay: bool | None = None,
        hide_url: bool | None = None,
        high_quality: bool | None = None,
        limit_user_songs_by: int | None = None,
        normalize_audio: bool | None = None,
        screensaver_timeout: int | None = None,
        show_splash_clock: bool | None = None,
        splash_delay: int | None = None,
        volume: float | None = None,
        enable_title_tidy: bool | None = None,
    ) -> None:
        """Initialize the Karaoke instance.

        Args:
            port: HTTP server port number.
            download_path: Directory path for downloaded songs.
            hide_url: Hide URL and QR code on splash screen.
            hide_notifications: Disable notification popups.
            hide_splash_screen: Run in headless mode.
            high_quality: Download higher quality videos (up to 1080p).
            volume: Default volume level (0.0 to 1.0).
            normalize_audio: Apply loudness normalization.
            complete_transcode_before_play: Buffer entire file before playback.
            buffer_size: Transcode buffer size in KB.
            log_level: Logging level (e.g., logging.DEBUG).
            splash_delay: Seconds to wait between songs.
            youtubedl_proxy: Proxy URL for yt-dlp.
            logo_path: Custom logo image path.
            hide_overlay: Hide video overlay.
            screensaver_timeout: Screensaver activation delay in seconds.
            url: Override auto-detected URL.
            prefer_hostname: Use hostname instead of IP in URL.
            disable_bg_music: Disable background music.
            bg_music_volume: Background music volume (0.0 to 1.0).
            bg_music_path: Directory for background music files.
            bg_video_path: Path to background video file.
            disable_bg_video: Disable background video.
            disable_score: Disable score screen.
            limit_user_songs_by: Max songs per user in queue (0 = unlimited).
            avsync: Audio/video sync adjustment in seconds.
            config_file_path: Path to config.ini file.
            cdg_pixel_scaling: Enable CDG pixel scaling.
            streaming_format: Video streaming format ('hls' or 'mp4').
            browse_results_per_page: Number of search results per page.
            additional_ytdl_args: Additional yt-dlp command arguments.
            socketio: SocketIO instance for real-time event emission.
            preferred_language: Language code for UI (e.g., 'en', 'de_DE').
        """
        logging.basicConfig(
            format="[%(asctime)s] %(levelname)s: %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
            level=int(log_level),
        )

        # Initialize event system and preferences (foundation for all components)
        self.events = EventSystem()
        self.preferences = PreferenceManager(config_file_path, target=self)

        # Platform-specific initializations
        self.platform = get_platform()
        self.os_version = get_os_version()
        self.ffmpeg_version = get_ffmpeg_version()
        self.is_transpose_enabled = is_transpose_enabled()
        self.supports_hardware_h264_encoding = supports_hardware_h264_encoding()
        self.youtubedl_version = get_youtubedl_version()
        self.is_raspberry_pi = is_raspberry_pi()

        logging.info("PiKaraoke version: " + VERSION)

        # Set non-preference attributes (not stored in config)
        self.port = port
        self.hide_splash_screen = hide_splash_screen
        # Experimental launch-only gate: must be re-passed each run, never persisted to config.
        self.enable_mic_passthrough = enable_mic_passthrough
        self.download_path = download_path
        self.log_level = log_level
        self.youtubedl_proxy = youtubedl_proxy
        self.additional_ytdl_args = additional_ytdl_args
        self.logo_path = self.default_logo_path if logo_path is None else logo_path
        self.prefer_hostname = prefer_hostname
        self.bg_music_path = self.default_bg_music_path if bg_music_path is None else bg_music_path
        self.bg_video_path = self.default_bg_video_path if bg_video_path is None else bg_video_path
        self.streaming_format = streaming_format
        self.socketio = socketio
        self.url_override = url
        self.url = self.get_url()

        # Load all preference-driven attributes from config (with CLI overrides as fallback)
        cli_args = {k: v for k, v in locals().items() if k != "self"}
        self._load_preferences(**cli_args)

        # Log the settings to debug level
        self.log_settings_to_debug()

        # Initialize database, scanner, and song manager (startup runs at end of __init__)
        self.db = KaraokeDatabase()
        self.song_manager = SongManager(
            self.download_path, db=self.db, get_title_tidy=lambda: self.enable_title_tidy
        )
        self._scanner = LibraryScanner(self.db)
        self._sync_lock = threading.Lock()

        self.generate_qr_code()

        # Set preferred language from command line if provided (persists to config)
        if preferred_language:
            self.preferences.set("preferred_language", preferred_language)
            logging.info(f"Setting preferred language to: {preferred_language}")

        # Initialize playback controller for video playback and FFmpeg coordination
        self.playback_controller = PlaybackController(
            preferences=self.preferences,
            events=self.events,
            filename_from_path=self.song_manager.display_name_from_path,
            streaming_format=self.streaming_format,
        )

        # [FASE 1] Gerenciador de Lista Paralela de Cantores (Ordem de Chegada)
        # FASE 2: Ja amarrado com queue_manager via eventos do EventSystem abaixo.
        self.singer_manager = SingerManager()

        # ============================================================
        # [FASE 2 AMARRACAO CANTORES <-> FILA REAL / PLAYBACK]
        # Funcoes de bridge (chamadas nos eventos internos do EventSystem).
        # Sem tocar em queue_manager / playback_controller - tudo a partir dos
        # eventos ja existentes. NUNCA PULA MUSICA (REGRA SUPREMA).
        # ============================================================
        def _fase2_refresh_singer_statuses_from_queue():
            try:
                sm = self.singer_manager
                if not sm:
                    return
                queue_items = list(getattr(self.queue_manager, "queue", []) or [])
                now_user = getattr(self.playback_controller, "now_playing_user", None)
                np = self.get_now_playing()
                next_user = np.get("next_user") if np else None
                info = sm.refresh_statuses_from_queue(
                    queue_items=queue_items,
                    now_playing_user=now_user,
                    next_user=next_user,
                )
                try:
                    if self.socketio:
                        self.socketio.emit("singers_match_status", info, namespace="/")
                        self.socketio.emit("singers_updated", sm.to_dict(), namespace="/")
                        pending = sm.get_pending_calls()
                        self.socketio.emit(
                            "singers_pending_calls",
                            {
                                "count": len(pending),
                                "singers": [
                                    {"singer_id": s.singer_id, "name": s.name}
                                    for s in pending
                                ],
                            },
                            namespace="/",
                        )
                except Exception:
                    pass
            except Exception as exc:
                logging.debug("[FASE2] refresh_statuses_from_queue falhou: %s", exc)

        # [V32 FIX BUG FINAL 13/08 13:12]
        # RACE CONDITION MATOU a marcação SUNG por 3 dias:
        # Quando a música ACABA DE VERDADE, o playback_controller faz em ORDEM:
        #   1) ZERA now_playing_user = None
        #   2) EMITE evento "song_ended"
        # Então no callback _fase2_mark_singer_sung_now() → agora user=None →
        # NÃO MARCA NINGUÉM COMO SUNG! → Junior fica sem rounds_sung, sem
        # teve_vez, sem SET infalível → de volta para GRUPO 1 como #1 visual,
        # volta a ser o "próximo de novo", e Simone nunca tem vez.
        #
        # SOLUÇÃO V32: Guardar sempre aqui em cache (self._fase2_last_sung_user_cache)
        # o ÚLTIMO now_playing_user VÁLIDO que passou por aqui. Quando song_ended
        # vier e o playback disser user=None, usamos este último valor salvo —
        # ele é sempre o cantor que ACABOU de tocar, 100% garantido.
        # Atualizamos esse cache EM TODOS os eventos que sabem quem está tocando:
        # playback_started e now_playing_update (se o user novo não for None).
        self._fase2_last_sung_user_cache: str | None = None
        def _fase2_atualizar_cache_user_valido() -> None:
            try:
                pu = getattr(self.playback_controller, "now_playing_user", None)
                if pu and str(pu).strip():
                    self._fase2_last_sung_user_cache = str(pu).strip()
            except Exception:
                pass

        def _fase2_mark_singer_sung_now(event_payload=None):
            # [V36 - CORRECAO BUG 14:20 MUSICA PAROU NA METADE - MARCA SUNG ERRADO]
            #
            # Antes V32-V35: song_ended SEMPRE marcava SUNG, mesmo que a música
            # tivesse parado na metade por: stale_stream / watchdog_stuck_position /
            # start_timeout / corrompida / ffmpeg crash / etc.
            #
            # Consequencia ruim: cantor ganhou rounds_sung++, teve_vez_nesta_rodada
            # True, ID no SET infalível → ele PASSOU na rodada e nunca mais tem
            # vez nesta rodada, mesmo que ele não tenho cantado NADA de verdade!
            #
            # SOLUCAO V36: só marcar SUNG (teve vez de verdade) se o reason do
            # song_ended for um destes 2 motivos "deu a vez ao cantor":
            #   (A) reason == "complete"  → música acabou NATURALMENTE (último segundo)
            #   (B) reason == "skip"      → usuário clicou MANUALMENTE em pular/avançar
            #                                 (ele estava lá, teve sua vez de verdade)
            # Todos os outros motivos (parou na metade, travou, rede caiu) → NÃO
            # marca SUNG, NÃO zera cache, só refresh de fila normal. O cantor
            # continua no GRUPO1 → automaticamente ele é o "PRÓXIMO" de novo
            # (ordem sagrada, ele nao passou), e pode escolher outra música ou
            # re-tentar a mesma.
            reason_end = "unknown"
            try:
                if isinstance(event_payload, dict) and str(event_payload.get("reason") or "").strip():
                    reason_end = str(event_payload["reason"]).strip().lower()
            except Exception:
                pass
            pode_marcar_como_sung = (reason_end in ("complete", "skip"))
            try:
                sm = self.singer_manager
                if not sm:
                    return
                user = getattr(self.playback_controller, "now_playing_user", None)
                # [V32 BLOCO CORREÇÃO]
                # Se playback já tiver zerado user (acontece sempre, race condition),
                # cai no CACHE do último user válido que estava tocando.
                if not user and getattr(self, "_fase2_last_sung_user_cache", None):
                    from copy import copy
                    user = copy(self._fase2_last_sung_user_cache)
                    logging.info(
                        "[FASE2 V32 FALLBACK OK] now_playing_user ja estava None (zerado pelo controller), usando cache do ultimo user tocando = %r",
                        user,
                    )
                if user:
                    if pode_marcar_como_sung:
                        singer = sm.mark_singer_sung_by_queue_user(str(user))
                        if singer is not None:
                            logging.info(
                                "[FASE2 AUTOMATICO FIM MUSICA V36] music acabou (reason=%r) -> cantor marcado SUNG: %s (user_fila=%r)",
                                reason_end, singer.name, str(user),
                            )
                            # [V32] Após marcar SUNG com sucesso: ZERA o cache (evita
                            # marcar o mesmo user duas vezes em caso de double emit).
                            try:
                                self._fase2_last_sung_user_cache = None
                            except Exception:
                                pass
                            _fase2_refresh_singer_statuses_from_queue()
                        else:
                            logging.warning(
                                "[FASE2 V36] user %r nao casou com nenhum Singer paralelo (nao atualizou SUNG). pode_marcar=True, reason=%r, cache? %r",
                                str(user), reason_end, getattr(self, "_fase2_last_sung_user_cache", None)
                            )
                    else:
                        # [V36 - MUSICA PAROU NA METADE / ERRO QUALQUER]
                        # NÃO MARCA SUNG. Apenas damos um log WARNING avisando,
                        # refresh status (para o banner continuar apontando ele),
                        # e NÃO ZERA o cache.
                        logging.warning(
                            "[FASE2 V36 BOM: NAO MARCOU SUNG!] musica acabou prematuramente (reason=%r). Cantor=%r CONTINUA NO GRUPO1 desta rodada, sera o proximo de novo para tentar outra musica ou a mesma."
                            " (Nao ganhou rounds_sung, nao marcou teve_vez, nao entrou no SET infalivel).",
                            reason_end, str(user),
                        )
                        _fase2_refresh_singer_statuses_from_queue()
                else:
                    logging.warning(
                        "[FASE2 V36] sem user now_playing e cache também vazio. nao marca SUNG de ninguem. reason=%r pode_marcar=%r",
                        reason_end, bool(pode_marcar_como_sung)
                    )
            except Exception as exc:
                logging.debug("[FASE2] mark_singer_sung_now falhou: %s", exc)
                import traceback as _tb
                _tb.print_exc()

        # Event bridging: the coordinator wires manager events to the UI (SocketIO/notifications).
        self.events.on("notification", self.log_and_send)
        self.events.on(
            "queue_update",
            lambda: (
                (self.socketio.emit("queue_update", namespace="/") if self.socketio else None),
                _fase2_refresh_singer_statuses_from_queue(),
            )
        )
        self.events.on(
            "preload_update",
            lambda: self.socketio.emit(
                "preload_update",
                self.playback_controller.get_preload_status_map(False) if self.playback_controller else {},
                namespace="/"
            ) if self.socketio else None
        )
        self.events.on("now_playing_update", self.update_now_playing_socket)
        # [V32 ATUALIZA CACHE ULTIMO USER TOCANDO - OBRIGATORIO]
        self.events.on("now_playing_update", _fase2_atualizar_cache_user_valido)
        self.events.on("now_playing_update", _fase2_refresh_singer_statuses_from_queue)
        self.events.on("playback_started", self.update_now_playing_socket)
        # [V32 ATUALIZA CACHE ULTIMO USER TOCANDO - OBRIGATORIO]
        self.events.on("playback_started", _fase2_atualizar_cache_user_valido)
        # ============== CORRECAO BUG SEQUENCIA CANTORES (Simone → pula para Joao #4) ==============
        # ANTES ERRADO: playback_started disparava mark_singer_sung_now() → MARCAVA o cantor como
        # "ja cantou / fim da rodada" ANTES MESMO DE ELE COMECAR A CANTAR! Isso baguncava tudo:
        #   #1 estava cantando agora → ja tava marcado SUNG → proximo ia pro #2,
        #   mas se Randomizer ou sistema disparava playback_started varias vezes sem musica de
        #   verdade, ia marcando SIMONE e MARIA como SUNG sem nunca terem cantado!
        # CORRETO: play_back STARTED = soh refresh status UI (marcar WAITING / now playing).
        # MARK_SUNG_SO_NO_FIM = SONG_ENDED: SIM, a musica ACABOU = ele realmente JA CANTOU!
        self.events.on(
            "playback_started",
            lambda: _fase2_refresh_singer_statuses_from_queue()
        )
        self.events.on("song_ended", self.update_now_playing_socket)
        # CORRETO: song_ended → marca o user como SUNG (ja cantou de verdade) + refresh
        self.events.on(
            "song_ended",
            lambda: (_fase2_mark_singer_sung_now(), _fase2_refresh_singer_statuses_from_queue())
        )

        # [V11 FIX-A BUG2 SEM SOM] Novo evento: stream URL mudou por RETRY do FFmpeg
        # (mesma musica, NOVA URL de stream). Broadcast via socket.io para todos
        # os clients Splash Master trocarem hls.loadSource(NOVA_URL) IMEDIATAMENTE,
        # sem esperar o now_playing_update ou o playback_started.
        self.events.on(
            "playback_stream_url_changed",
            lambda payload_dict: (
                self.socketio.emit("playback_stream_url_changed", payload_dict, namespace="/"),
                self.update_now_playing_socket(),
            ) if self.socketio else None
        )
        self.events.on("skip_requested", lambda: self.playback_controller.skip(False))
        self.events.on("song_downloaded", self.song_manager.register_download)
        self.events.on(
            "sync_started",
            lambda: self.socketio.emit("sync_started", namespace="/") if self.socketio else None,
        )
        self.events.on(
            "sync_finished",
            lambda: self.socketio.emit("sync_finished", namespace="/") if self.socketio else None,
        )

        # Initialize microphone manager for server-side mic passthrough
        self.sound_manager = SoundManager(
            preferences=self.preferences,
            events=self.events,
            enabled=self.enable_mic_passthrough,
        )
        self.sound_manager.start()

        # Initialize queue manager
        self.queue_manager = QueueManager(
            preferences=self.preferences,
            events=self.events,
            get_now_playing_user=lambda: self.playback_controller.now_playing_user,
            filename_from_path=self.song_manager.display_name_from_path,
            get_available_songs=lambda: self.song_manager.songs,
            singer_manager=self.singer_manager,
        )

        # Initialize and start download manager
        self.download_manager = DownloadManager(
            events=self.events,
            preferences=self.preferences,
            song_manager=self.song_manager,
            queue_manager=self.queue_manager,
            download_path=self.download_path,
            youtubedl_proxy=self.youtubedl_proxy,
            additional_ytdl_args=self.additional_ytdl_args,
        )
        self.download_manager.start()

        # Song library startup: warm cache from DB or blocking cold scan
        paths = self.db.get_all_song_paths()
        if paths:
            self.song_manager.songs.update(paths)
            logging.info("Loaded songs from database, syncing in the background")
            self.sync_library()
        else:
            logging.info("No existing database found, scanning song directory")
            result = self._scanner.scan(self.download_path)
            self._apply_scan_result(result)

    def _apply_scan_result(self, result: ScanResult) -> None:
        """Update SongList and emit notifications after a scan."""
        if result.added or result.moved or result.deleted:
            self.song_manager.songs.update(self.db.get_all_song_paths())
            parts = [
                label
                for count, label in [
                    (result.added, f"{result.added} added"),
                    (result.moved, f"{result.moved} moved"),
                    (result.deleted, f"{result.deleted} removed"),
                ]
                if count
            ]
            self.events.emit("notification", f"Library updated: {', '.join(parts)}", "success")

        if result.circuit_tripped:
            logging.error(
                f"Circuit breaker tripped: >50% of songs missing. "
                f"Drive may be unmounted: {self.download_path}"
            )
            self.events.emit(
                "notification",
                f"Song scan halted: too many songs missing. "
                f"Check your song directory: {self.download_path}. "
                "Click 'Sync Now' to retry after fixing.",
                "danger",
            )
            return

        logging.info(f"Scan complete: {result}")

    def sync_library(self) -> bool:
        """Trigger a background library scan.

        Used for both warm startup reconciliation and admin 'Sync Now'.
        Returns False if a sync is already in progress.
        """
        if not self._sync_lock.acquire(blocking=False):
            return False
        self.events.emit("sync_started")
        thread = threading.Thread(target=self._background_sync, daemon=True)
        thread.start()
        return True

    def _background_sync(self) -> None:
        try:
            logging.info(f"Background library scan starting: {self.download_path}")
            result = self._scanner.scan(self.download_path)
            self._apply_scan_result(result)
        finally:
            self._sync_lock.release()
            self.events.emit("sync_finished")

    def _load_preferences(self, **cli_overrides: Any) -> None:
        """Load preference-driven attributes from config file.

        Priority: CLI argument (if provided) > config file > PreferenceManager.DEFAULTS
        """
        self.preferences.apply_all(**cli_overrides)

    def get_url(self):
        """Get the URL for accessing the PiKaraoke web interface.

        On Raspberry Pi, retries getting the IP address for up to 30 seconds
        in case the network is still initializing at startup.

        Returns:
            URL string in format http://ip:port
        """
        if self.is_raspberry_pi:
            # retry in case pi is still starting up
            # and doesn't have an IP yet (occurs when launched from /etc/rc.local)
            end_time = int(time.time()) + 30
            while int(time.time()) < end_time:
                addresses_str = (
                    subprocess.check_output(["hostname", "-I"]).strip().decode("utf-8", "ignore")
                )
                addresses = addresses_str.split(" ")
                self.ip = addresses[0]
                if len(self.ip) < 7:
                    logging.debug("Couldn't get IP, retrying....")
                else:
                    break
        else:
            self.ip = get_ip(self.platform)

        logging.debug("IP address (for QR code and splash screen): " + self.ip)

        if self.url_override != None:
            logging.debug("Overriding URL with " + self.url_override)
            url = self.url_override
        else:
            if self.prefer_hostname:
                url = f"http://{socket.getfqdn().lower()}:{self.port}"
            else:
                url = f"http://{self.ip}:{self.port}"
        return url

    def log_settings_to_debug(self) -> None:
        """Log all current settings at debug level."""
        output = ""
        for key, value in sorted(vars(self).items()):
            output += f"  {key}: {value}\n"
        logging.debug("\n\n" + output)

    def generate_qr_code(self) -> None:
        """Generate a QR code image for the web interface URL."""
        logging.debug("Generating URL QR code")
        qr = qrcode.QRCode(
            version=1,
            box_size=1,
            border=4,
        )
        qr.add_data(self.url)
        qr.make()
        img = qr.make_image(image_factory=PyPNGImage)
        # Use writable data directory instead of program directory.
        # Include the port so multiple instances on the same host don't
        # overwrite each other's QR code (see issue #836).
        data_dir = get_data_directory()
        self.qr_code_path = os.path.join(data_dir, f"qrcode-{self.port}.png")
        img.save(self.qr_code_path)  # type: ignore[arg-type]

    def send_notification(self, message: str, color: str = "primary") -> None:
        """Send a notification to the web interface.

        Args:
            message: Notification message text.
            color: Bulma color class (primary, warning, success, danger).
        """
        # Color should be bulma compatible: primary, warning, success, danger
        hide_notifications = self.preferences.get_or_default("hide_notifications")
        if not hide_notifications:
            # don't allow new messages to clobber existing commands, one message at a time
            # other commands have a higher priority
            if self.now_playing_notification != None:
                return
            self.now_playing_notification = message + "::is-" + color
            # Emit notification via SocketIO for event-driven architecture
            if self.socketio:
                self.socketio.emit("notification", self.now_playing_notification, namespace="/")

    def log_and_send(self, message: str, category: str = "info") -> None:
        """Log a message and send it as a notification.

        Args:
            message: Message to log and display.
            category: Message category (info, success, warning, danger).
        """
        # Category should be one of: info, success, warning, danger
        if category == "success":
            logging.info(message)
            self.send_notification(message, "success")
        elif category == "warning":
            logging.warning(message)
            self.send_notification(message, "warning")
        elif category == "danger":
            logging.error(message)
            self.send_notification(message, "danger")
        else:
            logging.info(message)
            self.send_notification(message, "primary")

    def transpose_current(self, semitones: int) -> None:
        """Restart the current song with a new transpose value.

        Args:
            semitones: Number of semitones to transpose.
        """
        filename = self.playback_controller.now_playing_filename
        user = self.playback_controller.now_playing_user
        now_playing = self.playback_controller.now_playing

        if filename is None or user is None:
            logging.warning("Cannot transpose: no song currently playing")
            return
        # MSG: Message shown after the song is transposed, first is the semitones and then the song name
        self.log_and_send(_("Transposing by %s semitones: %s") % (semitones, now_playing))
        # Requeue the current song at the front with the new transpose value so it restarts next.
        self.queue_manager.enqueue(filename, user, semitones, True, log_action=False)
        self.playback_controller.skip(log_action=False)

    def volume_change(self, vol_level: float) -> bool:
        """Set the volume level.

        Args:
            vol_level: Volume level (0.0 to 1.0).

        Returns:
            True after setting volume.
        """
        self.volume = vol_level
        # MSG: Message shown after the volume is changed, will be followed by the volume level
        self.log_and_send(_("Volume: %s") % (int(self.volume * 100)))
        self.update_now_playing_socket()
        return True

    def vol_up(self) -> None:
        """Increase volume by 10%."""
        new_vol = min(self.volume + 0.1, 1.0)
        self.volume_change(new_vol)
        logging.debug(f"Increasing volume by 10%: {self.volume}")

    def vol_down(self) -> None:
        """Decrease volume by 10%."""
        new_vol = max(self.volume - 0.1, 0.0)
        self.volume_change(new_vol)
        logging.debug(f"Decreasing volume by 10%: {self.volume}")

    def restart(self) -> bool:
        """Restart the current song from the beginning.

        Returns:
            True if successful, False if nothing playing.
        """
        if self.playback_controller.is_playing:
            now_playing = self.playback_controller.now_playing
            logging.info("Restarting: " + (now_playing or "unknown song"))
            self.playback_controller.is_paused = False
            self.update_now_playing_socket()
            return True
        else:
            logging.warning("Tried to restart, but no file is playing!")
            return False

    def stop(self) -> None:
        """Stop the karaoke run loop."""
        self.sound_manager.stop()
        self.running = False

    def handle_run_loop(self) -> None:
        """Handle one iteration of the main run loop with a sleep interval."""
        time.sleep(self.loop_interval / 1000)

    def reset_now_playing_notification(self) -> None:
        """Clear the current notification."""
        self.now_playing_notification = None

    def reset_now_playing(self) -> None:
        """Reset all now playing state to defaults."""
        self.playback_controller.reset_now_playing()
        self.volume = self.preferences.get_or_default("volume")
        self.update_now_playing_socket()

    def get_now_playing(self) -> dict[str, Any]:
        """Get the current playback state.

        RETORNO DEFENSIVO V7: ANTES de devolver now_playing_url para clients,
        valida com validate_now_playing_alive() que arquivo EXISTE e ffmpeg
        esta healthy. Se STALE, invalidamos URL (nunca enviamos URL morta
        para clients que gerara 404 loop infinito no Splash).

        Returns:
            Dictionary with now playing info, queue preview, and volume.
        """
        queue = self.queue_manager.queue
        next_song = queue[0] if queue else None

        # Get playback state from PlaybackController
        playback_state = self.playback_controller.get_now_playing()

        # ----- NOVO V7: stale state sanitizer -----
        try:
            alive_status = self.playback_controller.validate_now_playing_alive()
        except Exception as exc:
            logging.debug("validate_now_playing_alive exception in get_now_playing: %s", exc)
            alive_status = "ok"

        if alive_status == "stale":
            now_url = playback_state.get("now_playing_url")
            logging.warning(
                "[get_now_playing STALE SANITIZER] now_playing_url=%s detectado STALE em "
                "get_now_playing. ZERANDO URL para nao enviar 404 infinito aos clients. "
                "Playback watchdog logo dispara end_song(stale_stream_detected) e toca a proxima.",
                now_url,
            )
            playback_state["now_playing_url"] = None
            playback_state["now_playing_subtitle_url"] = None
            playback_state["is_playing"] = False
            playback_state["now_playing"] = playback_state.get("now_playing") or None
            playback_state["now_playing_position"] = None

        return {
            **playback_state,
            "up_next": next_song["title"] if next_song else None,
            "next_user": next_song["user"] if next_song else None,
            "volume": self.volume,
        }

    def update_now_playing_socket(self) -> None:
        """Emit now_playing state change via SocketIO."""
        if self.socketio:
            self.socketio.emit("now_playing", self.get_now_playing(), namespace="/")

    def run(self) -> None:
        """Main run loop - processes queue and plays songs.

        OTIMIZACAO: apos iniciar uma musica, dispara preload da PROXIMA em
        background. Em cada iteracao do loop, se queue[0] mudou (alguem removeu
        ou adicionou nova na frente), atualiza o preload.
        """
        logging.debug("Starting PiKaraoke run loop")
        logging.info(f"Connect the player host to: {self.url}/splash")
        self.running = True
        last_window_signature: tuple | None = None
        while self.running:
            try:
                # Clean up if playback ended but state wasn't reset
                # ============================================================
                # [V50 CORRECAO RACE CONDITION FIM DE MUSICA - 13/08 18:12]
                # O PLAYBACK zera is_playing=False ANTES de emitir "song_ended".
                # A ordem SEMPRE era (ATE AGORA):
                #   1) playback faz: is_playing=False  (sem evento nenhum ainda)
                #   2) Run Loop roda neste intervalo de ~20ms: enxerga is_playing=False
                #      -> chama get_next_singer_that_should_sing_now()
                #      -> teve_vez_nesta_rodada DO JUNIOR AINDA ESTA False (pois o
                #         evento song_ended NAO chegou, que e quem marcava!)
                #      -> get_next retorna Junior de NOVO como proximo
                #      -> pop_next_only_for_singer(Junior) pega 2a musica
                #      -> TOCA A 2a MUSICA DO JUNIOR PULANDO SIMONE!!!
                #   3) 20-50ms DEPOIS: evento "song_ended" finalmente chega, marca
                #      teve_vez=True -> MAS E TARDE DEMAIS.
                #
                # CORRECAO V50 ABSOLUTA:
                # ANTES de resetar now_playing (e de deixar o Run Loop ver
                # is_playing=False e calcular proximo), nos MESMOS aqui no bloco
                # de Clean Up, SE tiver now_playing_user VALIDO:
                #   - Chama SingerManager.mark_teve_vez_RACE_QUICK_by_queue_user(user)
                #   - MARCA TEVE_VEZ=TRUE + SET infalivel IMEDIATAMENTE,
                #     ANTES que o Run Loop faca o proximo calculo de proximo.
                # Assim, o proximo get_next() JA VAI VER Junior com teve_vez=True
                # e NUNCA MAIS retorna ele de novo nesta rodada. Simone sempre e
                # a proxima (ordem #2 chegada).
                # ============================================================
                _tem_sm_agora = bool(getattr(self, "singer_manager", None))
                if _tem_sm_agora and not self.playback_controller.is_playing and (
                    self.playback_controller.now_playing is not None
                    or getattr(self, "_fase2_last_sung_user_cache", None)
                ):
                    _user_que_estava_tocando = getattr(
                        self.playback_controller, "now_playing_user", None
                    )
                    if (
                        not _user_que_estava_tocando
                        and getattr(self, "_fase2_last_sung_user_cache", None)
                    ):
                        _user_que_estava_tocando = getattr(self, "_fase2_last_sung_user_cache", None)
                    if _user_que_estava_tocando and str(_user_que_estava_tocando).strip():
                        try:
                            _u = str(_user_que_estava_tocando).strip()
                            _sm_v50 = getattr(self, "singer_manager", None)
                            if _sm_v50 is not None and hasattr(
                                _sm_v50, "mark_teve_vez_RACE_QUICK_by_queue_user"
                            ):
                                _s = _sm_v50.mark_teve_vez_RACE_QUICK_by_queue_user(
                                    _u,
                                    reason="race-pre-reset-antes-run-loop"
                                )
                                if _s is not None:
                                    logging.warning(
                                        "[V50 RACE FIX PRE-RESET OK] "
                                        "Cantor=%r user_fila=%r. teve_vez marcado "
                                        "ANTES do reset_now_playing e ANTES do "
                                        "proximo get_next. Nao toca 2a musica "
                                        "deste cantor sem passar a vez.",
                                        getattr(_s, "name", ""), _u
                                    )
                        except Exception as exc_v50:
                            logging.warning(
                                "[V50 RACE FIX PRE-RESET falhou (ignorado, "
                                "nao trava o playback): %s", exc_v50
                            )

                if (
                    not self.playback_controller.is_playing
                    and self.playback_controller.now_playing is not None
                ):
                    self.reset_now_playing()

                # ============================================================
                # [LEI ABSOLUTA VEZ SAGRADA - NUNCA MAIS PULA ORDEM DE CHEGADA]
                # A ORDEM DE CHEGADA DOS CANTORES (#1, #2, #3, #4) É A LEI.
                # SÓ a música do CANTOR DA VEZ pode tocar AGORA.
                # Se o cantor da vez NÃO TEM NENHUMA MÚSICA NA FILA:
                #  → NINGUÉM toca (MESMO QUE A FILA TENHA 100 MÚSICAS DE OUTROS).
                #  → Sistema bloqueia e CHAMA o cantor obrigatório URGENTE.
                #
                # ATENCAO 2026-08-13: ESTE BLOCO RODA SEMPRE QUE NAO ESTIVER
                # TOCANDO, MESMO QUE len(fila)==0. Motivo: se Simone eh proxima
                # obrigatoria e nao tem musica, mesmo com fila vazia o sistema
                # PRECISA TRAVAR e chamar ela, nao ficar silencioso.
                # ============================================================
                _sm = getattr(self, "singer_manager", None)
                song = None
                _prox_cantor_nome_display = ""
                _cantor_bloqueado_sem_musica = False
                _tem_sm_ativo = (_sm is not None and len(getattr(_sm, "_singers", []) or []) > 0)
                if not self.playback_controller.is_playing:
                    if _tem_sm_ativo:
                        _nowu = getattr(self.playback_controller, "now_playing_user", None)
                        _qit = list(getattr(self.queue_manager, "queue", []) or [])
                        _prox_cantor, _prox_nome = _sm.get_next_singer_that_should_sing_now(
                            _nowu, _qit
                        )
                        if _prox_cantor is not None:
                            _prox_cantor_nome_display = str(_prox_nome)
                            logging.info(
                                "[LEI SAGRADA RUN LOOP] Tentando obter musica do "
                                "PROXIMO OBRIGATORIO DA VEZ: singer_name=%r singer_id=%s "
                                "fila_qtd=%d",
                                _prox_cantor_nome_display,
                                str(getattr(_prox_cantor, "singer_id", "?")),
                                len(_qit),
                            )
                            song = self.queue_manager.pop_next_only_for_singer(_prox_cantor)
                            if song is None:
                                _cantor_bloqueado_sem_musica = True
                    # FALLBACK: se NÃO TEM singer manager (modo pikaraoke antigo)
                    # → usa a ordem NORMAL da fila real.
                    # CORRECAO 13/08: QUANDO TEM SM_ATIVO, NAO TEM MAIS FALLBACK
                    # pop_next()! Nunca mais pula ordem de chegada.
                    if not _tem_sm_ativo and song is None and not _cantor_bloqueado_sem_musica:
                        if len(self.queue_manager.queue) > 0:
                            song = self.queue_manager.pop_next()

                    # ===== BLOQUEIO (se nao tem musica) =====
                    if not song:
                        if _cantor_bloqueado_sem_musica and _prox_cantor_nome_display:
                            # NOTIFICAO URGENTE throttled 10s
                            agora = time.time()
                            _ult = float(getattr(self, "_vez_sagrada_last_block_notif", 0.0))
                            if (agora - _ult) >= 10.0:
                                self._vez_sagrada_last_block_notif = agora
                                _fila_qtd = len(getattr(self.queue_manager, "queue", []) or [])
                                if _fila_qtd > 0:
                                    msg_urg = (
                                        f"🚨🚨🚨 [VEZ SAGRADA BLOQUEADA] A PRÓXIMA VEZ É DE: "
                                        f"'{_prox_cantor_nome_display}' (ordem de chegada). "
                                        f"⚠️ TEM {_fila_qtd} MÚSICA(S) NA FILA, MAS NENHUMA É DELE/ELA! "
                                        f"O que fazer: (A) Adicione a música com o MESMO NOME do card do cantor "
                                        f"('{_prox_cantor_nome_display}') — ou — (B) na página /singers, abra o card dele "
                                        f"e CLIQUE EM '✏️ Adicionar alias' para linkar o NOME ERRADO QUE VOCÊ USOU "
                                        f"QUANDO ADICIONOU A MÚSICA (ex: 'Simone Silva na fila → alias no card'). "
                                        f"⚠️ NINGUÉM VAI CANTAR ATÉ QUE '{_prox_cantor_nome_display}' tenha "
                                        f"uma música dele/ela na fila OU você clique em 'Pular rodada' no card dele."
                                    )
                                else:
                                    msg_urg = (
                                        f"🚨🚨🚨 [VEZ SAGRADA BLOQUEADA] A PRÓXIMA VEZ É DE: "
                                        f"'{_prox_cantor_nome_display}' (ordem de chegada). "
                                        f"ELE/ELA AINDA NÃO ADICIONOU NENHUMA MÚSICA! "
                                        f"⚠️ NINGUÉM VAI CANTAR ATÉ QUE '{_prox_cantor_nome_display}' "
                                        f"ADICIONE UMA MÚSICA OU CLIQUE EM 'PULAR RODADA' no card dele. "
                                        f"Não adianta os outros cantores adicionarem música antes: "
                                        f"o sistema OBEDECE A ORDEM DE CHEGADA SEMPRE."
                                    )
                                self.log_and_send(msg_urg, "danger")
                                try: self.events.emit("queue_update")
                                except Exception: pass
                                try: self.events.emit("now_playing_update")
                                except Exception: pass
                                logging.error(msg_urg)
                        # Proxima iteracao do loop (nao toca nada, trava aguardando)
                        self.playback_controller.log_output()
                        self.handle_run_loop()
                        continue

                # ============== TEM MUSICA (song != None): VAI TOCAR AGORA ==============
                if song is not None and not self.playback_controller.is_playing:
                    self.reset_now_playing()
                    # Splash delay between songs
                    splash_delay = self.preferences.get_or_default("splash_delay")
                    i = 0
                    while i < (splash_delay * 1000):
                        self.handle_run_loop()
                        i += self.loop_interval

                    # Avisa clientes que pop_next removeu 1a musica
                    try: self.events.emit("queue_update")
                    except Exception: pass

                    t0 = time.time()
                    # ====== [V9 CAMADA DUPLA DE SEGURANÇA - BACKUP] ======
                    song_file = str(song.get("file") or "")
                    ok_e, msg_e, resolved_p = QueueManager.validate_song_file_exists(song_file)
                    if not ok_e:
                        user_name = str(song.get("user") or "")
                        logging.error(
                            "[RUN LOOP V9 CANCEL PLAY] Tentou tocar musica mas arquivo NAO EXISTE no disco. "
                            "user=%s file=%r msg=%s",
                            user_name, song_file, msg_e[:260]
                        )
                        self.log_and_send(
                            (
                                f"⚠️ A música de '{user_name}' **não pôde ser tocada porque o arquivo NÃO EXISTE no servidor Dell**. "
                                f"Ela foi automaticamente cancelada (não foi para o watchdog, não vai pular). "
                                f"Arquivo: {song_file!r}. "
                                f"Provavelmente o download do YouTube cancelou/falhou ou o arquivo foi apagado do diretório songs/. "
                                f"Peça para o cantor tentar novamente (outra música ou re-download)."
                            ),
                            "danger",
                        )
                        try:
                            self.events.emit("notification", msg_e, "danger")
                        except Exception:
                            pass
                        self.events.emit("queue_update")
                        self.events.emit("now_playing_update")
                        self.playback_controller.log_output()
                        self.handle_run_loop()
                        continue
                    if resolved_p:
                        song["file"] = resolved_p
                        song_file = resolved_p

                    result = self.playback_controller.play_file(
                        song_file, song["user"], song["semitones"]
                    )
                    lat_ms = (time.time() - t0) * 1000.0
                    if not result.success and result.error:
                        self.log_and_send(result.error, "danger")
                        logging.warning(
                            "[LATENCIA FAIL] Falhou iniciar %s | user=%s | %.0fms | erro=%s",
                            song["file"].split("/")[-1], song["user"], lat_ms, str(result.error)[:160]
                        )
                    else:
                        logging.info(
                            "[LATENCIA QUEUE -> PLAYBACK START] %s | user=%s | total %.0fms",
                            song["file"].split("/")[-1], song["user"], lat_ms
                        )
                    # Atualiza UI NA HORA (agora que mudou o now playing)
                    try: self.events.emit("now_playing")
                    except Exception: pass
                    try: self.events.emit("now_playing_update")
                    except Exception: pass
                    try: self.events.emit("queue_update")
                    except Exception: pass
                    last_window_signature: tuple | None = None

                # --- NOVO: Mantem JANELA DE 5 PRELOADS sempre atualizada ---
                # Substitui o preload_next(queue[0]) unico anterior: agora temos as 5 proximas
                # prontas em background, MAX_PARALLEL=1 (Dell seguro, sem travar a musica atual).
                q = self.queue_manager.queue
                if len(q) > 0:
                    from pikaraoke.lib.queue_manager import QueueManager as _QM
                    # Garantir qid em todos itens da janela (para itens criados antes do patch)
                    window_raw = q[:self.playback_controller.PRELOAD_WINDOW_MAX]
                    for it in window_raw:
                        _QM.ensure_item_qid(it)
                    # Assinatura = tupla de (qid, file, semitones) para detectar mudancas na fila
                    sig = tuple(
                        (str(it.get("qid") or ""), str(it.get("file") or ""), int(it.get("semitones") or 0))
                        for it in window_raw
                    )
                    if sig != last_window_signature:
                        self.playback_controller.refresh_queue_window(window_raw)
                        last_window_signature = sig
                else:
                    if last_window_signature is not None:
                        # Fila ficou vazia: cancela todos preloads pendentes para economizar CPU
                        self.playback_controller._cancel_any_pending_preload()
                    last_window_signature = None

                self.playback_controller.log_output()
                self.handle_run_loop()
            except KeyboardInterrupt:
                logging.warning("Keyboard interrupt: Exiting pikaraoke...")
                self.running = False
