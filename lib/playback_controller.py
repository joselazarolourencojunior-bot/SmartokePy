"""Playback controller for managing video playback state and coordination."""

import logging
import os
import threading
import time
from typing import TYPE_CHECKING, Any, Callable

from flask_babel import _

from pikaraoke.lib._debug_points import debug_point
from pikaraoke.lib.events import EventSystem
from pikaraoke.lib.file_resolver import delete_tmp_dir, get_tmp_dir
from pikaraoke.lib.preference_manager import PreferenceManager
from pikaraoke.lib.stream_manager import PlaybackResult, StreamManager

if TYPE_CHECKING:
    import subprocess


class PlaybackController:
    """Controller for managing playback state and stream coordination.

    Owns all "now playing" state and coordinates with StreamManager for
    FFmpeg transcoding and playback.

    NOVAS PROTECAO E OTIMIZACOES (contra travamentos e lentidao):
    - Watchdog progresso playback: se playback_position NAO AVANCA > STUCK_SECONDS
      (ex: 12s) enquanto is_playing=True => pula para proxima musica automaticamente.
    - Timeout no inicio do play: se apos play_file() o cliente (splash) nao
      chamar start_song em PLAY_START_TIMEOUT_S => encerra a musica por timeout.
    - Preload da proxima musica (preload_next / get_preloaded) para transicao
      rapida entre musicas (ja deixa FFmpeg/transcode rodando em segundo plano).
    - Medidas de latencia / performance logadas automaticamente.

    Attributes:
        now_playing: Title of the currently playing song.
        now_playing_filename: File path of the currently playing song.
        now_playing_user: User who queued the current song.
        now_playing_transpose: Semitones to transpose current song.
        now_playing_duration: Duration of current song in seconds.
        now_playing_url: Stream URL for current song.
        now_playing_subtitle_url: URL path for subtitles.
        now_playing_position: Current playback position in seconds.
        is_paused: Whether playback is paused.
        is_playing: Whether a song is currently playing.
        ffmpeg_process: Currently running FFmpeg subprocess.
    """

    # ----- NOVOS parametros de protecao e performance -----
    PLAY_START_TIMEOUT_S: int = 90         # [FIX SUPREMO] antes 25s → 90s.  SE dwload ok (source_exists=True), NUNCA pula por esse timeout.
    STUCK_NO_PROGRESS_SECONDS: int = 45    # [FIX SUPREMO] antes 18s → 45s. SE dwload ok → NUNCA pula por progress stuck (só retry ffmpeg).
    STUCK_POLL_INTERVAL_S: float = 2.0     # frequencia watchdog progresso
    PRELOAD_ENABLED: bool = True           # pre-transcode das proximas musicas (janela)
    # --- Novos para scheduler multi-preload ---
    PRELOAD_WINDOW_MAX: int = 2            # Dell antigo 3GB RAM: JANELA MINIMA (so 2 proximas). Antes era 5, estourava RAM/SWAP.
    MAX_PARALLEL_PRELOADS: int = 1         # 1 = seguro para Dell antigo (nao trava CPU). NUNCA > 2.
    PRELOAD_STATUS_LABELS: dict[str, str] = {
        "unknown":     "Aguardando",
        "queued":      "Na fila para carregar",
        "transcoding": "Carregando agora...",
        "ready":       "Pronta e carregada",
        "failed":      "Falhou ao carregar",
    }
    PRELOAD_STATUS_COLORS: dict[str, str] = {
        "unknown":     "#94a3b8",   # cinza
        "queued":      "#94a3b8",   # cinza
        "transcoding": "#fbbf24",   # amarelo
        "ready":       "#34d399",   # verde
        "failed":      "#f87171",   # vermelho
    }
    # -------------------------------------------------------

    now_playing: str | None = None
    now_playing_filename: str | None = None
    now_playing_user: str | None = None
    now_playing_transpose: int = 0
    now_playing_duration: int | None = None
    now_playing_url: str | None = None
    now_playing_subtitle_url: str | None = None
    now_playing_position: float | None = None
    is_paused: bool = True
    is_playing: bool = False

    def __init__(
        self,
        preferences: PreferenceManager,
        events: EventSystem,
        filename_from_path: Callable[[str, bool], str],
        streaming_format: str = "hls",
    ) -> None:
        """Initialize the playback controller.

        Args:
            preferences: PreferenceManager instance for configuration.
            events: EventSystem instance for event emission.
            filename_from_path: Function to extract display name from path.
            streaming_format: Video streaming format ('hls' or 'mp4').
        """
        self.preferences = preferences
        self.events = events
        self.filename_from_path = filename_from_path
        self.stream_manager = StreamManager(preferences, streaming_format)

        # ---- NOVOS: state para watchdogs + preload (janela multipla) ----
        self._last_position_value: float | None = None
        self._last_position_seen_at: float | None = None
        self._play_requested_at: float | None = None   # quando play_file foi chamado
        # [FIX SUPREMO V8] ANTES: bool 1 retry só. AGORA: contador + backoff.
        # SE ARQUIVO FONTE EXISTIR → retry INFINITO, NUNCA PULA.
        self._transcode_retry_count_for_song: int = 0
        self._transcode_last_retry_at: float | None = None
        self._stuck_retry_count_for_song: int = 0  # contador para stuck progress retries
        self._watchdog_thread: threading.Thread | None = None
        self._watchdog_stop: threading.Event = threading.Event()
        self._lock = threading.RLock()
        # --- NOVO: scheduler multi-preload (janela das N proximas) ---
        # Chave primaria = (file_path: str, semitones: int). Tambem temos qid para UI.
        # _preload_window[key] = {qid, file, semitones, status, stream_manager, result,
        #                         exception, thread, thread_done_event, created_at, updated_at}
        self._preload_window: dict[tuple[str, int], dict[str, Any]] = {}
        self._preload_qid_to_key: dict[str, tuple[str, int]] = {}   # qid -> chave primaria
        self._preload_window_lock = threading.Lock()
        self._preload_emit_debounce_timer: threading.Timer | None = None
        self._ensure_watchdog_running()

    def _ensure_watchdog_running(self) -> None:
        """Garante thread de watchdog esteja viva (1 vez)."""
        if self._watchdog_thread and self._watchdog_thread.is_alive():
            return
        self._watchdog_stop.clear()
        self._watchdog_thread = threading.Thread(
            target=self._watchdog_loop, name="pikaraoke-playback-watchdog", daemon=True
        )
        self._watchdog_thread.start()
        logging.info("Playback watchdog iniciado.")

    def _watchdog_loop(self) -> None:
        """Thread background: (1) timeout no inicio, (2) trava progresso playback."""
        try:
            while not self._watchdog_stop.is_set():
                try:
                    self._watchdog_tick()
                except Exception as e:
                    logging.warning("Playback watchdog tick error: %s", e)
                self._watchdog_stop.wait(self.STUCK_POLL_INTERVAL_S)
        except Exception as e:
            logging.error("Playback watchdog thread exit with error: %s", e)

    def _watchdog_tick(self) -> None:
        now = time.time()

        # --- (0) STALE STREAM DETECTOR (novo V8 SUPREMO) ---
        # REGRA SUPREMA DO USUARIO: se dwload arquivo (now_playing_filename) existe → NUNCA PULA.
        # So pode end_song() se source_exists=False (nao tem nem arquivo fonte para tocar).
        stale_snap = self.validate_now_playing_alive()
        if stale_snap == "stale":
            # stale = arquivo fonte TAMBEM NAO EXISTE. Nao tem outra opcao, PULA.
            logging.warning(
                "[WATCHDOG STALE STREAM] now_playing_url=%s nao existe no disco, "
                "FFmpeg NAO rodando, E arquivo fonte TAMBEM NAO EXISTE (stale PID). "
                "Pulando (sem outra opcao).",
                self.now_playing_url,
            )
            try:
                self.events.emit(
                    "notification",
                    _("Música corrompida ou desatualizada. Pulando..."),
                    "warning",
                )
            except Exception:
                pass
            self.end_song(reason="stale_stream_detected")
            return
        if stale_snap == "waiting_ffmpeg_data":
            pass
        if stale_snap == "ffmpeg_dead_source_ok":
            # ====== USUARIO TEM RAZAO: dwload ok = NAO PULA, RETRY INFINITO ======
            source_exists = bool(self.now_playing_filename and os.path.isfile(self.now_playing_filename))
            if source_exists:
                # backoff: 0.5s * min(retry_count+1, 8).  Max ~4s entre retries.
                backoff_s = 0.5 * min(self._transcode_retry_count_for_song + 1, 8)
                last_r = self._transcode_last_retry_at
                if last_r is None or (now - last_r) >= backoff_s:
                    self._transcode_retry_count_for_song += 1
                    rc = self._transcode_retry_count_for_song
                    logging.warning(
                        "[WATCHDOG V8] dwload OK (fonte existe). Tentativa #%d restart FFmpeg. "
                        "Backoff %.1fs. NAO VAMOS PULAR, nunca. file=%s",
                        rc, backoff_s, self.now_playing_filename,
                    )
                    try:
                        self.events.emit(
                            "notification",
                            _(f"Reconectando música (tentativa {rc})..."),
                            "info",
                        )
                    except Exception:
                        pass
                    ok = self._retry_transcode_current_song()
                    self._transcode_last_retry_at = now
                    if ok:
                        # novo transcode iniciou. Reset também _play_requested_at p/ renovar timeout de 90s.
                        with self._lock:
                            if self._play_requested_at is not None:
                                self._play_requested_at = now
                        return
                return  # NUNCA cai para timeout se dwload ok

        # --- (1) TIMEOUT INICIO DE MUSICA (antes chamava end_song) ---
        # [FIX SUPREMO V8] SE source_exists=True → NUNCA end_song. Apenas reseta timer + retry ffmpeg.
        with self._lock:
            rp = self._play_requested_at
            isp = self.is_playing
            np_fn = self.now_playing_filename
            paused = self.is_paused
            cur_pos = self.now_playing_position

        source_exists = bool(np_fn and os.path.isfile(np_fn))
        if rp is not None and not isp:
            waited = now - rp
            if waited > self.PLAY_START_TIMEOUT_S:
                if not source_exists:
                    # Nao tem arquivo fonte para tocar. Ultimo recurso: pula.
                    logging.warning(
                        "[WATCHDOG TIMEOUT START] play_file %.1fs, start_song NAO chegou, "
                        "E dwload NAO EXISTE (sem arquivo fonte). Pulando. file=%s",
                        waited, np_fn
                    )
                    try:
                        self.events.emit(
                            "notification",
                            _("Demorou para abrir a música (splash desconectado?). Pulando..."),
                            "warning",
                        )
                    except Exception:
                        pass
                    self.end_song(reason="watchdog_start_timeout_no_source")
                    return
                else:
                    # dwload OK → NÃO PULA! Apenas dá mais 90s + retry ffmpeg se estiver morto.
                    self._transcode_retry_count_for_song += 1
                    rc = self._transcode_retry_count_for_song
                    logging.warning(
                        "[WATCHDOG V8 TIMEOUT START RENOVADO] dwload OK (fonte existe). "
                        "%.1fs sem start_song → renovando timer + tentativa #%d restart FFmpeg. NAO PULA. file=%s",
                        waited, rc, np_fn
                    )
                    try:
                        self.events.emit(
                            "notification",
                            _(f"Ainda carregando música (tentativa {rc})..."),
                            "info",
                        )
                    except Exception:
                        pass
                    ffmpeg_proc = self.stream_manager.ffmpeg_process
                    ffmpeg_alive = bool(ffmpeg_proc and ffmpeg_proc.poll() is None)
                    if not ffmpeg_alive:
                        self._retry_transcode_current_song()
                    self._transcode_last_retry_at = now
                    with self._lock:
                        self._play_requested_at = now  # renova 90s novos
                    return

        # --- (2) TRAVAMENTO MEIO DA MUSICA (progresso estagnado) ---
        # [FIX SUPREMO V8] SE source_exists=True → NUNCA PULA, só retry ffmpeg (kill + restart com nova URL).
        if isp and not paused:
            with self._lock:
                last_v = self._last_position_value
                last_t = self._last_position_seen_at
                if cur_pos != last_v:
                    self._last_position_value = cur_pos
                    self._last_position_seen_at = now
                    last_v = cur_pos
                    last_t = now
            delta_s = None
            if last_t is not None and last_v is not None:
                stuck_s = now - last_t
                delta_s = stuck_s
                if stuck_s > self.STUCK_NO_PROGRESS_SECONDS:
                    #region debug-point P5 stuck trigger fired
                    try:
                        debug_point(
                            "P5_watchdog_STUCK_TRIGGER",
                            now_position=cur_pos,
                            last_saved_position=last_v,
                            seconds_since_progress_change=stuck_s,
                            stuck_threshold_s=self.STUCK_NO_PROGRESS_SECONDS,
                            is_playing=isp,
                            is_paused=paused,
                            now_playing_filename=np_fn,
                            source_exists=source_exists,
                            reason_for_check="watchdog_tick_2_V8",
                        )
                    except Exception:
                        pass
                    #endregion
                    if not source_exists:
                        logging.warning(
                            "[WATCHDOG STUCK] posicao estagnada %.1fs E dwload NAO EXISTE. Pulando. file=%s",
                            stuck_s, np_fn
                        )
                        try:
                            self.events.emit(
                                "notification",
                                _("Música travou. Pulando para próxima..."),
                                "danger",
                            )
                        except Exception:
                            pass
                        self.end_song(reason="watchdog_stuck_position_no_source")
                        return
                    else:
                        # dwload OK → NÃO PULA! Kill FFmpeg, recomeça transcode, atualiza URL.
                        self._stuck_retry_count_for_song += 1
                        sr = self._stuck_retry_count_for_song
                        logging.warning(
                            "[WATCHDOG V8 STUCK FIX] dwload OK (fonte existe). Posicao estagnada %.1fs "
                            "→ Restart FFmpeg #%d, NOVA URL stream, NAO PULA. file=%s",
                            stuck_s, sr, np_fn
                        )
                        try:
                            self.events.emit(
                                "notification",
                                _(f"Música parou. Recuperando automaticamente (tentativa {sr})..."),
                                "warning",
                            )
                        except Exception:
                            pass
                        ffmpeg_proc = self.stream_manager.ffmpeg_process
                        if ffmpeg_proc and ffmpeg_proc.poll() is None:
                            try:
                                ffmpeg_proc.kill()
                                try: ffmpeg_proc.wait(timeout=2)
                                except Exception: pass
                            except Exception:
                                pass
                        self._retry_transcode_current_song()
                        # reset stuck state, e tambem renova timer de inicio (pois mudou URL)
                        with self._lock:
                            self._last_position_value = None
                            self._last_position_seen_at = None
                            if self._play_requested_at is not None:
                                self._play_requested_at = now
                        return
                else:
                    # region debug-point P5 regular progress snap
                    try:
                        snap_key = int(now // 6)
                        _last_snap = getattr(self, "_dbg_p5_snap_key", None)
                        if _last_snap != snap_key or stuck_s > (self.STUCK_NO_PROGRESS_SECONDS * 0.6):
                            self._dbg_p5_snap_key = snap_key
                            debug_point(
                                "P5_watchdog_progress_snap",
                                now_position=cur_pos,
                                last_saved_position=last_v,
                                seconds_since_progress_change=stuck_s,
                                stuck_threshold_s=self.STUCK_NO_PROGRESS_SECONDS,
                                is_playing=isp,
                                is_paused=paused,
                                now_playing_filename=np_fn,
                                source_exists=source_exists,
                                reason_for_check="watchdog_tick_2_every6s_V8",
                            )
                    except Exception:
                        pass
                    #endregion
            else:
                pass
        else:
            with self._lock:
                self._last_position_value = None
                self._last_position_seen_at = None

    @property
    def ffmpeg_process(self) -> "subprocess.Popen | None":
        """Get the current FFmpeg process."""
        return self.stream_manager.ffmpeg_process

    # ----- METODOS NOVOS V7: stale stream detector + estado healthy -----
    def _stream_url_to_disk_path(self, stream_url: str | None) -> str | None:
        """Converte /stream/<id>.m3u8 (ou .mp4) em path absoluto no disco.

        Returns path se puder extrair id com seguranca, None caso contrario.
        """
        if not stream_url:
            return None
        # Normaliza: rota Flask = /stream/<id.ext> ; serve do get_tmp_dir()
        prefix = "/stream/"
        if not stream_url.startswith(prefix):
            return None
        rest = stream_url[len(prefix):]
        if not rest or "/" in rest or ".." in rest:
            return None  # directory traversal ou formato invalido
        tmp_dir = get_tmp_dir()
        return os.path.join(tmp_dir, rest)

    def validate_now_playing_alive(self) -> str:
        """Verifica se now_playing_url corresponde a um arquivo EXISTENTE em disco,
        e se FFmpeg/playback esta num estado HEALTHY (nao morto).

        RETORNOS (VERSAO CORRIGIDA V7.1 - conforme usuario: nao pula se dwload ok!):
          'ok'          -> tudo certo, stream pronto ou tocando.
          'waiting_ffmpeg_data' -> ffmpeg VIVO, arquivo ainda NAO foi criado
                           (esperando FFmpeg escrever manifesto/init). Normal.
                           Watchdog de play_start_timeout cobre o resto.
          'ffmpeg_dead_source_ok' -> FFmpeg MORREU, mas arquivo FONTE baixado
                           (now_playing_filename) AINDA EXISTE. NAO PULAR MUSICA.
                           Devemos RETRY ffmpeg. Usuario com razao: "dwload arquivo
                           esta ok, tem que tocar".
          'stale'       -> now_playing_url NAO EXISTE em disco E FFmpeg MORTO
                           E ARQUIVO FONTE (now_playing_filename) NAO EXISTE
                           (stale state apos restart servico com URL de PID antigo,
                            nao tem como tocar). PODE PULAR.
          'nothing_playing' -> is_playing=False e nao tem now_playing_url setada
                           (normal, nada para validar).
        """
        if not self.now_playing_url and not self.is_playing:
            return "nothing_playing"

        # 1) Verifica ffmpeg
        proc = self.stream_manager.ffmpeg_process
        ffmpeg_alive = bool(proc and proc.poll() is None)

        # 2) Verifica arquivo stream (.m3u8) no disco
        url: str | None = self.now_playing_url
        disk_path = self._stream_url_to_disk_path(url)
        file_exists = bool(disk_path and os.path.isfile(disk_path))
        manifest_size = 0
        if file_exists:
            try:
                manifest_size = os.path.getsize(disk_path)
                file_exists = file_exists and manifest_size > 16
            except OSError:
                file_exists = False
                manifest_size = 0

        # 3) Verifica ARQUIVO FONTE (dwload MP4) - O MAIS IMPORTANTE, USUARIO TEM RAZAO
        # Se ele existe SEMPRE podemos tentar tocar de novo (retry ffmpeg se morreu)
        source_fn = self.now_playing_filename
        source_exists = bool(source_fn and os.path.isfile(source_fn))

        if file_exists:
            result = "ok"
        elif ffmpeg_alive:
            # Manifesto ainda nao gravado → FFmpeg esta preparando, esperar
            result = "waiting_ffmpeg_data"
        elif source_exists:
            # Arquivo baixado existe, mas FFmpeg morreu sem escrever manifesto
            # (crash codec / OOM / erro). NAO PULAR, RETRY FFMPEG.
            result = "ffmpeg_dead_source_ok"
        elif self.now_playing_url or self._play_requested_at is not None or self.is_playing:
            result = "stale"
        else:
            result = "nothing_playing"

        #region debug-point P6
        try:
            debug_point(
                "P6_validate_alive_result",
                result_string=result,
                ffmpeg_alive=ffmpeg_alive,
                manifest_exists=file_exists,
                manifest_size_bytes=manifest_size,
                source_exists=source_exists,
                now_playing_url=url,
                now_playing_filename=source_fn,
                disk_path=disk_path,
            )
        except Exception:
            pass
        #endregion
        return result

    def _retry_transcode_current_song(self) -> bool:
        """FFmpeg morreu mas arquivo fonte existe. Roda ffmpeg de novo.

        NAO chama end_song (nao pula musica). Retorna True se novo transcode OK.
        """
        try:
            with self._lock:
                source_fn = self.now_playing_filename
                semitones = self.now_playing_transpose or 0
                url_before = self.now_playing_url
            if not source_fn or not os.path.isfile(source_fn):
                logging.debug("[retry_transcode] source_fn ausente. Nao faz nada.")
                return False
            logging.warning(
                "[RETRY TRANSCODE] FFmpeg morreu mas source=%s existe (dwload OK). "
                "Restart ffmpeg (sem pular musica, usuario tem razao).",
                source_fn,
            )
            try:
                self.stream_manager.kill_ffmpeg()
            except Exception:
                pass
            time.sleep(0.35)
            result = self.stream_manager.play_file(source_fn, semitones)
            if not result.success:
                logging.error("[RETRY TRANSCODE] falhou: %s", result.error)
                return False
            with self._lock:
                self.now_playing_url = result.stream_url
                self.now_playing_subtitle_url = result.subtitle_url
                self._play_requested_at = time.time()
                # [V10 FIX BUG2 SEM SOM]
                # Depois do retry, temos NOVA URL de stream. O SPLASH MASTER
                # (browser) continua segurando a URL ANTIGA no hls.js → ele toca
                # imagem congelada (ou só vídeo) e SEM ÁUDIO, porque a URL velha
                # não produz mais segmentos.
                # Resetar:
                #  _last_position_value = None (para nao considerar stuck ainda)
                #  _last_position_seen_at = None
                #  _play_requested_at = now (pois temos que esperar cliente NOVAMENTE
                #    agora conectar na NOVA URL)
                #  E O MAIS IMPORTANTE: zera is_playing para que quando o cliente
                #  conectar na URL NOVA, ele vai chamar start_song() de novo (que
                #  seta is_playing=True).
                self._last_position_value = None
                self._last_position_seen_at = None
                self._transcode_last_retry_at = time.time()
                was_playing_before = bool(self.is_playing)
                self.is_playing = False
            logging.info(
                "[RETRY TRANSCODE OK] nova URL=%s antiga=%s. Aguardando splash CONECTAR NA URL NOVA.",
                result.stream_url, url_before,
            )
            try:
                self.events.emit(
                    "notification",
                    _("Arquivo ok, reiniciando reprodução (conectando na nova stream)..."),
                    "info",
                )
            except Exception:
                pass
            # ================================================================
            # [V10 FIX BUG2 CRITICO]
            # Evento: playback_started + now_playing_update.
            # karaoke.update_now_playing_socket() EMITE socketio "now_playing"
            # para TODOS clients, com a NOVA URL /stream/<novo id>.m3u8.
            # O Splash Master (receptor) deve pegar a NOVA URL e trocar
            # hls.js.loadSource(NOVA_URL) - SEM PULAR MÚSICA, SEM VOLTAR AO
            # COMEÇO, SEM NOTIFICAR O USUÁRIO COMO SE FOSSE MÚSICA NOVA.
            # Para distinguir: adicionamos campo _v10_retry_url=True no evento.
            # ================================================================
            try:
                self.events.emit(
                    "playback_stream_url_changed",
                    {
                        "new_url": result.stream_url,
                        "old_url": url_before,
                        "new_subtitle_url": result.subtitle_url,
                        "filename": self.now_playing_filename,
                        "retry_count": self._transcode_retry_count_for_song,
                    },
                )
            except Exception:
                pass
            try:
                self.events.emit("now_playing_update")
                if was_playing_before:
                    # So dispara playback_started se estava tocando mesmo.
                    self.events.emit("playback_started")
            except Exception:
                pass
            return True
        except Exception as exc:
            logging.error("[RETRY TRANSCODE] exception: %s", exc)
            return False

    def play_file(self, file_path: str, user: str, semitones: int = 0) -> PlaybackResult:
        """Start playback of a media file.

        Blocks until client connects or timeout occurs.

        OTIMIZACAO: primeiro tenta reaproveitar um preload (proxima musica
        pre-transcodificada em background) para comecar quase instantaneamente.

        Args:
            file_path: Path to the media file to play.
            user: User who queued the song.
            semitones: Number of semitones to transpose (0 = no change).

        Returns:
            PlaybackResult with success status and stream information.
        """
        t0 = time.time()
        source_exists = bool(file_path and os.path.isfile(file_path))
        #region debug-point P1
        try:
            debug_point(
                "P1_play_file_called",
                file_path=file_path,
                user=user,
                semitones=semitones,
                source_exists=source_exists,
            )
        except Exception:
            pass
        #endregion
        if not source_exists:
            error_msg = _("Song file not found: %s") % file_path
            logging.warning(error_msg)
            return PlaybackResult(success=False, error=error_msg)

        logging.info(
            f"Playing file: {file_path} for user: {user}, transposed {semitones} semitones"
        )

        # ---- NOVO: tenta usar preload (se for o mesmo arquivo/semitons) ----
        preloaded = self._take_preload(file_path, semitones)
        preload_hit = bool(preloaded)
        if preloaded:
            result, pre_sm = preloaded
            logging.info(
                "[PRELOAD HIT] Usando preload para %s. Economia de latencia: stream manager pronto antes!",
                file_path
            )
            # Troca stream manager atual pelo pre-carregado (evita rodar ffmpeg do zero)
            self.stream_manager = pre_sm
            self._cancel_any_pending_preload()
        else:
            logging.info("[PRELOAD MISS] Sem preload pronto para %s; iniciando transcode agora.", file_path)
            result = self.stream_manager.play_file(file_path, semitones)

        # ---- NOVO: Retentativa 1x se transcode falhar ----
        if not result.success:
            logging.warning("Primeira tentativa transcode falhou. Tentando uma 2a vez antes de abortar...")
            try:
                self.stream_manager.kill_ffmpeg()
            except Exception:
                pass
            time.sleep(0.4)
            result2 = self.stream_manager.play_file(file_path, semitones)
            if result2.success:
                result = result2
                logging.info("Segunda tentativa transcode OK (recuperado).")
            else:
                # tenta sem transcode (caminho direto) se falhou
                return result2 if not result2.success else result2

        if not result.success:
            with self._lock:
                self._play_requested_at = None
            return result

        with self._lock:
            self.now_playing = self.filename_from_path(file_path, remove_youtube_id=True)
            self.now_playing_filename = file_path
            self.now_playing_user = user
            self.now_playing_transpose = semitones
            self.now_playing_duration = result.duration
            self.now_playing_url = result.stream_url
            self.now_playing_subtitle_url = result.subtitle_url
            self.is_paused = False
            self._last_position_value = None
            self._last_position_seen_at = None
            # [FIX SUPREMO V8] nova musica = zera contadores retry (INFINITOS por musica
            self._transcode_retry_count_for_song = 0
            self._transcode_last_retry_at = None
            self._stuck_retry_count_for_song = 0
            self._play_requested_at = time.time()   # WATCHDOG (1) TIMEOUT start_song

        self.events.emit("playback_started")

        # Wait for client to connect (ANTIGO: max 10s. NOVO: loop de 12s + watchdog 25s cobre o resto, nao fica eterno aqui)
        local_timeout = 12.0
        start_w = time.time()
        while not self.is_playing:
            elapsed = time.time() - start_w
            if elapsed >= local_timeout:
                break
            time.sleep(0.08)

        if not self.is_playing:
            logging.warning(
                "[play_file] %.1fs sem cliente dar start_song. Nao abortamos ainda => "
                "deixa o watchdog (timeout 10s) decidir pular.", local_timeout
            )

        tdelta = (time.time() - t0) * 1000.0
        logging.info(
            "[LATENCIA play_file] %s | user=%s | latencia_total=%.0fms | is_playing=%s",
            file_path.split("/")[-1], user, tdelta, self.is_playing
        )
        return PlaybackResult(
            success=result.success,
            stream_url=result.stream_url,
            subtitle_url=result.subtitle_url,
            duration=result.duration,
            error=result.error,
        ) if result.success else result

    def start_song(self, stream_id_marker: str | None = None) -> None:
        """Mark the current song as actively playing.

        Called by Flask route when client connects to stream.
        Idempotent - safe to call multiple times.

        PROTECAO EXTRA: so marca como started se:
          (1) existe musica corrente (now_playing_filename != None) e
          (2) play_file() foi chamado recentemente (_play_requested_at != None) ou
              a musica ja estava tocando (is_playing=True).
        Chamadas stale (requisicoes antigas de outra stream que chegaram atrasadas)
        sao IGNORADAS (ex: cliente splash pedindo URL de musica que acabou) para nao
        roubar o start da proxima musica nem zerar o watchdog timeout start.
        """
        is_playing_before = bool(self.is_playing)
        if self.is_playing:
            #region debug-point P4 idempotent
            try:
                debug_point(
                    "P4_start_song_idempotent",
                    stream_id_marker=str(stream_id_marker or "")[:60],
                    is_playing_before=is_playing_before,
                    now_playing_url=self.now_playing_url,
                )
            except Exception:
                pass
            #endregion
            return
        now = time.time()
        with self._lock:
            fn = self.now_playing_filename
            rp = self._play_requested_at
            np_url = self.now_playing_url
            # PROTECAO: ignora start_song se nao ha musica corrente OU
            # se play_file nao marcou request (chamada stale)
            if not fn or rp is None:
                #region debug-point P4 stale ignore
                try:
                    debug_point(
                        "P4_start_song_IGNORED_stale",
                        stream_id_marker=str(stream_id_marker or "")[:60],
                        now_playing_filename=fn,
                        _play_requested_at=rp,
                        now_playing_url=np_url,
                    )
                except Exception:
                    pass
                #endregion
                logging.info(
                    "start_song IGNORADO (stale?): now_playing_file=%s _play_requested_at=%s stream_id=%s",
                    fn, rp, str(stream_id_marker or "")[:40],
                )
                return
            wait_ms = (now - rp) * 1000.0
            self._play_requested_at = None
            self.is_playing = True
            self._last_position_value = 0.0
            self._last_position_seen_at = now

        # matches now_playing_url?
        sm_id = str(stream_id_marker or "")[:120]
        belongs_current = (np_url and len(sm_id) > 3 and (sm_id in (np_url or "") or np_url in sm_id))
        #region debug-point P4 called accepted
        try:
            debug_point(
                "P4_start_song_ACCEPTED",
                stream_id_marker=sm_id,
                _play_requested_at_age_ms=int(wait_ms),
                is_playing_before=is_playing_before,
                matches_now_playing_url=bool(belongs_current),
                now_playing_url=np_url,
                now_playing_filename=fn,
            )
        except Exception:
            pass
        #endregion
        logging.info(
            "Song starting: %s | start demorou %.0fms depois do play_file. stream_id=%s",
            self.now_playing, wait_ms, str(stream_id_marker or "")[:40],
        )

    def end_song(self, reason: str | None = None) -> None:
        """End the current song and clean up resources.

        Args:
            reason: Optional reason for ending (e.g., 'complete', 'skip', 'timeout').
        """
        #region debug-point P7 (ANTES DE MUDAR QUALQUER ESTADO)
        try:
            proc = self.stream_manager.ffmpeg_process if getattr(self, "stream_manager", None) else None
            ffmpeg_alive_before = bool(proc and proc.poll() is None)
            manifest_file = self._stream_url_to_disk_path(self.now_playing_url)
            manifest_exists_before = bool(manifest_file and os.path.isfile(manifest_file))
            source_exists_before = bool(self.now_playing_filename and os.path.isfile(self.now_playing_filename))
            import traceback
            stack = traceback.format_stack(limit=8)[-8:]
            debug_point(
                "P7_end_song_CALLED",
                reason=str(reason or ""),
                now_playing_title=self.now_playing,
                now_playing_filename_exists=source_exists_before,
                now_playing_filename_path=self.now_playing_filename,
                is_playing_before=bool(self.is_playing),
                ffmpeg_alive_before=ffmpeg_alive_before,
                manifest_exists_before=manifest_exists_before,
                manifest_path=manifest_file,
                now_playing_url=self.now_playing_url,
                _play_requested_at=bool(getattr(self, "_play_requested_at", None) is not None),
                _transcode_retry_count=getattr(self, "_transcode_retry_count_for_song", -1),
                _stuck_retry_count=getattr(self, "_stuck_retry_count_for_song", -1),
                caller_stack=" | ".join([s.strip().replace("\n", " ") for s in stack]),
            )
        except Exception as exc:
            logging.debug("debug_point_P7_exception: %s", exc)
        #endregion
        logging.info(f"Song ending: {self.now_playing}")
        if reason:
            logging.info(f"Reason: {reason}")
            if reason not in ("complete", "skip"):
                # MSG: Message shown when the song ends abnormally
                try:
                    self.events.emit("notification", _("Song ended abnormally: %s") % reason, "danger")
                except Exception:
                    pass

        with self._lock:
            self._play_requested_at = None
            self._last_position_value = None
            self._last_position_seen_at = None
            # [FIX SUPREMO V8] fim musica -> zera contadores retry
            self._transcode_retry_count_for_song = 0
            self._transcode_last_retry_at = None
            self._stuck_retry_count_for_song = 0

        self.reset_now_playing()
        try:
            self.stream_manager.kill_ffmpeg()
        except Exception as e:
            logging.warning("kill_ffmpeg erro em end_song (ignorado): %s", e)
        # Small delay to ensure FFmpeg fully terminates and file handles close
        # Critical on Raspberry Pi with slow SD cards and hardware encoder cleanup
        try:
            time.sleep(0.25)
            delete_tmp_dir()
        except Exception as e:
            logging.warning("cleanup erro em end_song (ignorado): %s", e)
        logging.debug("Cleanup complete")

        self.events.emit("song_ended", {"reason": str(reason or "unknown")})

    # =====================================================================
    # METODOS NOVOS: PRELOAD MULTIPLO (janela das N proximas musicas)
    #
    # Regras para NAO atrapalhar o playback atual no Dell antigo:
    #   - MAX_PARALLEL_PRELOADS = 1 (nunca roda 2 FFmpeg de preload ao mesmo
    #     tempo; isso garantiria zero impacto no CPU da musica tocando agora).
    #   - PRELOAD_WINDOW_MAX = 5 (mantemos preparadas apenas as 5 primeiras
    #     musicas da fila; resto fica como "unknown" ate subir na janela).
    # =====================================================================

    def preload_next(self, file_path: str, semitones: int = 0) -> None:
        """Método de compatibilidade (chamado por código antigo).

        Internamente vira um refresh_queue_window([1 item]) para não quebrar
        código que ainda usa .preload_next() (ex: loop karaoke.py antigo).
        """
        pseudo_item = {"qid": f"legacy_{int(time.time()*1000)}",
                       "file": file_path, "semitones": semitones}
        self.refresh_queue_window([pseudo_item])

    def _emit_preload_update(self) -> None:
        """Emite evento interno `preload_update` com debounce de ~80ms.

        Múltiplas mudanças rápidas (ex: adicionar 3 itens + startar 1 worker)
        resultarão em APENAS 1 socket.emit para o frontend, evitando spam.
        """
        if not self.PRELOAD_ENABLED:
            return
        try:
            if self._preload_emit_debounce_timer is not None:
                try: self._preload_emit_debounce_timer.cancel()
                except Exception: pass
            t = threading.Timer(0.08, self._do_emit_preload_update)
            t.daemon = True
            self._preload_emit_debounce_timer = t
            t.start()
        except Exception as exc:
            logging.debug("_emit_preload_update erro: %s", exc)

    def _do_emit_preload_update(self) -> None:
        """[run in debounce timer thread] Realiza o emit de fato."""
        try:
            self.events.emit("preload_update")
        except Exception as exc:
            logging.debug("preload_update event emit falhou: %s", exc)
        finally:
            self._preload_emit_debounce_timer = None

    def refresh_queue_window(self, queue_items: list[dict[str, Any]]) -> None:
        """Atualiza a janela de preloads com base na fila REAL atual.

        - Recebe os PRIMEIROS N itens da queue (geralmente queue[:5]).
        - Itens JÁ na janela: preserva (mantem estado / se esta rodando continua).
        - Itens NOVOS: marca status="queued" e adiciona na janela.
        - Itens ANTIGOS que sairam da fila: cancela (mata ffmpeg se houver).
        - Depois chama dispatch_next_pending() para iniciar o proximo se tiver vaga.

        Chamado em cada tick do loop Karaoke.run() (a cada ~500ms) quando a
        fila mudar ou periodicamente.

        Args:
            queue_items: lista de itens (dicts) com pelo menos: qid, file, semitones.
        """
        if not self.PRELOAD_ENABLED:
            return
        now = time.time()

        # Truncar para WINDOW_MAX (seguranca extra - mesmo que chamador passe mais)
        window_limit = self.PRELOAD_WINDOW_MAX
        items = queue_items[:window_limit] if len(queue_items) > window_limit else queue_items

        active_keys: set[tuple[str, int]] = set()
        active_qids: set[str] = set()
        for idx, it in enumerate(items):
            qid = str(it.get("qid") or f"auto_{idx}_{now}")
            fp = str(it.get("file") or "")
            st = int(it.get("semitones") or 0)
            if not fp or not os.path.isfile(fp):
                continue
            key: tuple[str, int] = (fp, st)
            active_keys.add(key)
            active_qids.add(qid)

            with self._preload_window_lock:
                existing = self._preload_window.get(key)
                if existing is not None:
                    # Ja existe: atualiza qid se precisar / priority idx
                    existing["queue_index"] = idx
                    existing["qid"] = qid
                    existing["updated_at"] = now
                    # Mapeamento qid -> key (sempre o ultimo prevalece)
                    self._preload_qid_to_key[qid] = key
                    continue

                # NOVO: monta entrada na janela
                entry: dict[str, Any] = {
                    "qid": qid,
                    "file": fp,
                    "semitones": st,
                    "status": "queued",
                    "queue_index": idx,
                    "stream_manager": None,
                    "thread": None,
                    "thread_done_event": threading.Event(),
                    "result": None,
                    "exception": None,
                    "created_at": now,
                    "updated_at": now,
                }
                self._preload_window[key] = entry
                self._preload_qid_to_key[qid] = key

                logging.info("[PRELOAD QUEUED #%d] %s semitones=%d  (qid=%s)",
                             idx, fp.split("/")[-1], st, qid[:8])

        # --- Cancela entradas STALE (que sairam da janela ativa) ---
        self._cancel_stale_window_entries(active_keys, active_qids)

        # --- Inicia proximo pendente, se tiver capacidade ---
        self._dispatch_next_pending_if_capacity()

        self._emit_preload_update()

    def _cancel_stale_window_entries(self,
                                     active_keys: set[tuple[str, int]],
                                     active_qids: set[str]) -> None:
        """Remove entradas da janela que não estão mais na fila ativa."""
        with self._preload_window_lock:
            keys_to_kill: list[tuple[str, int]] = [
                k for k in self._preload_window if k not in active_keys
            ]
            for k in keys_to_kill:
                entry = self._preload_window.pop(k, None)
                if entry is None:
                    continue
                # Tenta matar ffmpeg se tiver rodando
                sm = entry.get("stream_manager")
                if sm:
                    try: sm.kill_ffmpeg()
                    except Exception: pass
                qid = entry.get("qid")
                if qid and qid in self._preload_qid_to_key:
                    del self._preload_qid_to_key[qid]
                logging.debug("[PRELOAD DROPPED] %s semitones=%d (saiu da janela N)",
                              (k[0].split("/")[-1] if k else "?"), k[1] if len(k) > 1 else 0)

            # Limpa qids orfaos do mapa (seguranca)
            qids_to_del = [q for q in self._preload_qid_to_key if q not in active_qids]
            for q in qids_to_del:
                try: del self._preload_qid_to_key[q]
                except Exception: pass
        self._emit_preload_update()

    def _dispatch_next_pending_if_capacity(self) -> None:
        """Se (threads rodando agora < MAX_PARALLEL), inicia o item 'queued' de MAIOR prioridade (menor queue_index = mais perto de tocar)."""
        if not self.PRELOAD_ENABLED:
            return

        with self._preload_window_lock:
            # Conta quantos estao 'transcoding' AGORA
            count_running = sum(1 for e in self._preload_window.values()
                                if e.get("status") == "transcoding")
            if count_running >= self.MAX_PARALLEL_PRELOADS:
                return  # sem capacidade

            # Escolhe o CANDIDATO: status == 'queued' e menor queue_index
            candidates = [
                (key, entry) for key, entry in self._preload_window.items()
                if entry.get("status") == "queued"
            ]
            if not candidates:
                return  # nada pendente

            candidates.sort(key=lambda pair: int(pair[1].get("queue_index", 9999)))
            chosen_key, chosen = candidates[0]

            # Prepara stream manager e marca como transcoding
            chosen_sm = StreamManager(self.preferences, self.stream_manager.streaming_format)
            chosen["stream_manager"] = chosen_sm
            chosen["status"] = "transcoding"
            chosen["updated_at"] = time.time()
            chosen["thread_done_event"].clear()

            t = threading.Thread(
                target=self._multi_preload_worker,
                args=(chosen_key,),
                name=f"pikaraoke-preload-{chosen.get('queue_index',0)}-{chosen['qid'][:6]}",
                daemon=True,
            )
            chosen["thread"] = t

        # Fora do lock: starta a thread
        t.start()
        logging.info("[PRELOAD START] idx=%d %s semitones=%d (qid=%s). parallel=%d/%d",
                     chosen.get("queue_index", -1),
                     chosen["file"].split("/")[-1],
                     chosen["semitones"],
                     chosen["qid"][:8],
                     count_running + 1, self.MAX_PARALLEL_PRELOADS)
        self._emit_preload_update()

    def _multi_preload_worker(self, key: tuple[str, int]) -> None:
        """Worker (thread) que executa transcode de UM item da janela.

        Ao terminar: marca ready/failed e dispara dispatch de novo para pegar o proximo.
        """
        with self._preload_window_lock:
            entry = self._preload_window.get(key)
            if entry is None:
                return
            sm: StreamManager | None = entry.get("stream_manager")
            fp = entry["file"]
            st = entry["semitones"]
        if sm is None:
            return

        t0 = time.time()
        try:
            result = sm.play_file(fp, st)
            with self._preload_window_lock:
                entry = self._preload_window.get(key)
                if entry is not None:
                    entry["result"] = result
                    entry["status"] = "ready" if result.success else "failed"
                    entry["exception"] = None
                    entry["updated_at"] = time.time()
            if result.success:
                dt_ms = (time.time() - t0) * 1000
                logging.info("[PRELOAD READY] %s semitones=%d (%.0fms).",
                             fp.split("/")[-1], st, dt_ms)
            else:
                logging.warning("[PRELOAD FAILED] %s semitones=%d | %s",
                                fp.split("/")[-1], st,
                                str(result.error)[:160] if result.error else "sem msg")
        except Exception as exc:
            logging.warning("[PRELOAD EXCEPTION] %s semitones=%d | %s",
                            fp.split("/")[-1], st, exc)
            with self._preload_window_lock:
                entry = self._preload_window.get(key)
                if entry is not None:
                    entry["status"] = "failed"
                    entry["exception"] = exc
                    entry["updated_at"] = time.time()
        finally:
            with self._preload_window_lock:
                entry = self._preload_window.get(key)
                if entry is not None:
                    ev = entry.get("thread_done_event")
                    if ev is not None:
                        try: ev.set()
                        except Exception: pass
            # SEMPRE que um worker acaba, tenta disparar o proximo pendente!
            try:
                self._dispatch_next_pending_if_capacity()
            except Exception as d_exc:
                logging.debug("dispatch pos-worker falhou: %s", d_exc)
            self._emit_preload_update()

    def _take_preload(self, file_path: str, semitones: int) -> tuple[PlaybackResult, StreamManager] | None:
        """Se existe um preload READY para (file,semitones), consome e retorna.

        - Procura na janela por chave (file_path, semitones) == READY
        - Espera até 400ms se status == 'transcoding' (quase pronto, afim de economizar)
        - Se encontrado e sucesso: REMOVE da janela (consumido!) e retorna (result, stream_manager)
        - Caso contrario retorna None (play_file fara transcode do zero).
        """
        if not self.PRELOAD_ENABLED:
            return None

        key: tuple[str, int] = (str(file_path), int(semitones))
        consumed_entry: dict[str, Any] | None = None

        with self._preload_window_lock:
            entry = self._preload_window.get(key)
            if entry is None:
                return None

            st = entry.get("status")
            # Se ainda está carregando, esperamos POUCO (400ms) — se demorar mais,
            # melhor iniciar do zero do que deixar o público esperando.
            if st == "transcoding":
                ev: threading.Event | None = entry.get("thread_done_event")
                self._preload_window_lock.release()
                try:
                    if ev and not ev.is_set():
                        ev.wait(timeout=0.4)
                finally:
                    self._preload_window_lock.acquire()
                entry = self._preload_window.get(key)
                if entry is None:
                    return None

            # Agora checamos de novo depois da espera
            if entry.get("status") != "ready":
                return None

            res: PlaybackResult | None = entry.get("result")
            sm: StreamManager | None = entry.get("stream_manager")
            if res is None or sm is None or not res.success:
                # Nao serve: remove da janela
                self._cleanup_entry_from_window_locked(key)
                return None

            # ================================================================
            # [V10 FIX BUG1 - PRELOAD STALE]
            # Critico: um preload pode ter sido feito 1 minuto atrás (FFmpeg
            # transcodificou, gravou manifesto, e DEPOIS DE 31s o FFmpeg
            # TERMINOU (músicas curtas). Quando chegamos aqui o ffmpeg PROCESS
            # JA MORREU. play_file ia retornar success=True (temos manifesto),
            # mas SPLASH iria tocar uns segmentos e depois TRAVAR (sem
            # segmentos novos, pois ffmpeg MORREU).
            # ================================================================
            proc = getattr(sm, "ffmpeg_process", None)
            ffmpeg_still_running = bool(proc is not None and proc.poll() is None)
            if not ffmpeg_still_running:
                try:
                    manifest_exists = bool(res and res.stream_url)
                    u = (res.stream_url or "") if res else ""
                    logging.warning(
                        "[PRELOAD V10 STALE REJECTED] %s semitones=%d. "
                        "Stream manager FFmpeg NAO ESTA MAIS RODANDO (morreu antes do play_file!). "
                        "Nao vamos usar este preload - vamos re-rodar transcode do zero (ffmpeg vivo). stream_url=%s manifest=%s",
                        str(file_path).split("/")[-1][:80], semitones,
                        u[-64:], manifest_exists,
                    )
                except Exception:
                    pass
                self._cleanup_entry_from_window_locked(key)
                return None

            # Consome: retira da janela e retorna
            consumed_entry = entry
            self._cleanup_entry_from_window_locked(key)

        if consumed_entry is None:
            return None

        logging.info("[PRELOAD HIT] %s semitones=%d consumido da janela (transicao rapida!).",
                     file_path.split("/")[-1], semitones)
        self._emit_preload_update()
        return (res, sm)

    def _cleanup_entry_from_window_locked(self, key: tuple[str, int]) -> None:
        """[Chamar DENTRO do _preload_window_lock] Remove entrada e desaloca."""
        entry = self._preload_window.pop(key, None)
        if entry is None:
            return
        qid = entry.get("qid")
        if qid and self._preload_qid_to_key.get(qid) == key:
            try: del self._preload_qid_to_key[qid]
            except Exception: pass
        self._emit_preload_update()

    def _cancel_any_pending_preload(self) -> None:
        """Compatibilidade: cancela TODOS preloads da janela e zera estado.

        Chamado por play_file() quando acerta um preload hit (para limpar o resto).
        """
        with self._preload_window_lock:
            all_keys = list(self._preload_window.keys())
            for k in all_keys:
                entry = self._preload_window.get(k)
                if entry is None:
                    continue
                sm = entry.get("stream_manager")
                if sm is not None:
                    try: sm.kill_ffmpeg()
                    except Exception: pass
                th = entry.get("thread")
                if th is not None and th.is_alive():
                    ev = entry.get("thread_done_event")
                    if ev is not None:
                        try: ev.set()
                        except Exception: pass
            self._preload_window.clear()
            self._preload_qid_to_key.clear()
        self._emit_preload_update()

    def get_preload_status_map(self, include_meta: bool = True) -> dict[str, Any]:
        """Retorna mapa de status por qid (para UI / Maestro LEDs).

        Returns:
            dict com formato:
              {
                "by_qid": { "a1b2c3": "ready", "d4e5f6": "transcoding", ... },
                "labels": {"unknown": "...", "queued": "...", ...},  (se include_meta)
                "colors": {"unknown": "#94a3b8", ...},              (se include_meta)
                "window_size": 5, "max_parallel": 1,
                "running_count": N, "ready_count": M,
              }
        """
        out: dict[str, Any] = {
            "by_qid": {},
            "window_size": self.PRELOAD_WINDOW_MAX,
            "max_parallel": self.MAX_PARALLEL_PRELOADS,
            "running_count": 0,
            "ready_count": 0,
            "queued_count": 0,
            "failed_count": 0,
        }
        if include_meta:
            out["labels"] = dict(self.PRELOAD_STATUS_LABELS)
            out["colors"] = dict(self.PRELOAD_STATUS_COLORS)
        with self._preload_window_lock:
            for qid in self._preload_qid_to_key:
                key = self._preload_qid_to_key[qid]
                entry = self._preload_window.get(key)
                if entry is None:
                    continue
                st = entry.get("status") or "unknown"
                out["by_qid"][qid] = st
                if st == "transcoding":
                    out["running_count"] += 1
                elif st == "ready":
                    out["ready_count"] += 1
                elif st == "queued":
                    out["queued_count"] += 1
                elif st == "failed":
                    out["failed_count"] += 1
        return out

    def skip(self, log_action: bool = True) -> bool:
        """Skip the currently playing song.

        Args:
            log_action: Whether to log and notify about the skip.

        Returns:
            True if a song was skipped, False if nothing playing.
        """
        if self.is_playing:
            if log_action:
                # MSG: Message shown after the song is skipped, will be followed by song name
                self.events.emit("notification", _("Skip: %s") % self.now_playing, "info")
            self.end_song(reason="skip")
            return True
        else:
            logging.warning("Tried to skip, but no file is playing!")
            return False

    def pause(self) -> bool:
        """Toggle pause state of the current song.

        Returns:
            True if successful, False if nothing playing.
        """
        if self.is_playing:
            if self.is_paused:
                # MSG: Message shown after the song is resumed, will be followed by song name
                self.events.emit("notification", _("Resume: %s") % self.now_playing, "info")
            else:
                # MSG: Message shown after the song is paused, will be followed by song name
                self.events.emit("notification", _("Pause: %s") % self.now_playing, "info")
            self.is_paused = not self.is_paused
            self.events.emit("now_playing_update")
            return True
        else:
            logging.warning("Tried to pause, but no file is playing!")
            return False

    def get_now_playing(self) -> dict[str, str | int | float | bool | None]:
        """Get the current playback state.

        Returns:
            Dictionary with now playing information.
        """
        return {
            "now_playing": self.now_playing,
            "now_playing_user": self.now_playing_user,
            "now_playing_duration": self.now_playing_duration,
            "now_playing_transpose": self.now_playing_transpose,
            "now_playing_url": self.now_playing_url,
            "now_playing_subtitle_url": self.now_playing_subtitle_url,
            "now_playing_position": self.now_playing_position,
            "is_paused": self.is_paused,
        }

    def reset_now_playing(self) -> None:
        """Reset all now playing state to defaults."""
        self.now_playing = None
        self.now_playing_filename = None
        self.now_playing_user = None
        self.now_playing_url = None
        self.now_playing_subtitle_url = None
        self.is_paused = True
        self.is_playing = False
        self.now_playing_transpose = 0
        self.now_playing_duration = None
        self.now_playing_position = None
        # [FIX SUPREMO V8] reset flags retry infinito
        self._transcode_retry_count_for_song = 0
        self._transcode_last_retry_at = None
        self._stuck_retry_count_for_song = 0

    def log_output(self) -> None:
        """Log any pending FFmpeg output."""
        self.stream_manager.log_ffmpeg_output()
