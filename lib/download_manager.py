"""Download queue manager for serialized video downloads."""

from __future__ import annotations

import contextlib
import logging
import os
import re
import subprocess
import time
import uuid
from queue import Empty, Queue
from threading import Thread

from pikaraoke.lib.events import EventSystem
from pikaraoke.lib.preference_manager import PreferenceManager
from pikaraoke.lib.queue_manager import QueueManager
from pikaraoke.lib.song_manager import SongManager
from pikaraoke.lib.ffmpeg import validate_media_file
from pikaraoke.lib.metadata_parser import normalize_for_comparison, regex_tidy
from pikaraoke.lib.youtube_dl import (
    build_ytdl_download_command,
    get_video_metadata,
    get_youtube_id_from_url,
    is_likely_karaoke_result,
    normalize_youtube_url_to_std,
)


class DownloadManager:
    """Manages a queue of video downloads, processing them serially.

    This prevents rate limiting from download sources and reduces CPU load
    by ensuring only one download runs at a time.

    Attributes:
        download_queue: Queue holding pending download requests.
    """

    # ============================================================
    # TRADUCOES PT-BR: dicionario para mensagens de erro de download
    # (usado por classificar_mensagem_erro ABAIXO). Os matchers sao
    # case-insensitive e procurar por substring na mensagem bruta.
    # UMA UNICA LISTA, usada TANTO para UI permanente QUANTO para log.
    # ============================================================
    PTBR_ERROR_RULES: list[tuple[tuple[str, ...], str]] = [
        # === MUSICA JA EXISTIA / DUPLICADA ===
        (("already exists in library",),
         "Música já existia na biblioteca (não precisa baixar de novo)"),
        # === FFMPEG / BINARIO FALTANDO ===
        (("ffmpeg not found", "ffmpeg is not installed", "avconv not found"),
         "FFmpeg não está instalado no servidor (impossível converter/formatar o vídeo — instale o ffmpeg no Dell)"),
        # === ERRO DE CONFIGURACAO / CLI DO yt-dlp (parametro invalido, syntax errado) ===
        (("invalid http retry sleep expression", "invalid fragment retry sleep expression",
          "unrecognized arguments", "__main__.py: error:", "usage: yt-dlp",
          "error: no such option", "valueerror:", "typeerror:"),
         "Erro INTERNO de configuração do yt-dlp (parâmetro/expressão inválido na linha de comando — verificar a versão e sintaxe dos args)"),
        # === REGRA ESPECIAL PRIORIDADE MAXIMA: yt-dlp do Dell achou o formato (existe!),
        # mas na HORA DE BAIXAR os bytes do video/audio, YouTube bloqueou com HTTP 403/4xx.
        # CONDIÇÃO: TEM "unable to download video data" E também TEM "http error" ou "forbidden".
        # Esta COMBINACAO NUNCA significa "formato nao existe" ou "video removido/privado".
        # O USUARIO confirmou: NO SEU NAVEGADOR (Chrome) o VIDEO RODA NORMAL.
        # O problema eh APENAS o servidor Dell (yt-dlp) bloqueado de baixar o formato.
        (("unable to download video data:",),
         "YouTube BLOQUEOU o servidor Dell de BAIXAR este formato de vídeo (assinatura de URL expirou para o IP do Dell ou YouTube detectou o yt-dlp). "
         "ESSE VÍDEO FUNCIONA NORMAL no seu navegador Chrome do PC — o bloqueio é só no Dell via programa de download. "
         "SOLUÇÕES: (1) Tente novamente em 1~2 minutos (muda a assinatura), ou (2) Use o botão 'Link Avançado' na página de busca que baixa sem análise e pode contornar, "
         "ou (3) Baixe diretamente no seu PC com Chrome e coloque na pasta das músicas manualmente"),
        # === FALTA DE JS RUNTIME (yt-dlp precisa de Node.js/Deno/Bun/QuickJS para CAPTCHA) ===
        (("no supported javascript runtime could be found",
          "js challenge providers: bun (unavailable), deno (unavailable), node (unavailable)",
          "to use another runtime add  --js-runtimes",
          "youtube extraction without a js runtime has been deprecated"),
         "SERVIDOR DELL FALTA: Node.js/Deno/Bun não está instalado! O YouTube está pedindo CAPTCHA/JS challenge e o yt-dlp não consegue resolver sem um runtime JavaScript. Instale Node.js LTS no Dell (apt install nodejs npm)"),
        # === CAPTCHA / BOT DETECTADO (Sign in to confirm you're not a bot) ===
        (("sign in to confirm you're not a bot", "sign in to confirm youâ€™re not a bot",
          "sign in to confirm you're not a bot", "you're not a bot",
          "not a bot", "captcha", "bot detected", "recaptcha", "js challenge failed",
          "unable to solve challenge", "could not get js player",
          "player responses have no formats", "raise_no_formats", "logically required",
          "login_required"),
         "YouTube detectou BOT/CAPTCHA e pediu login/prova que não é robô. Use o 'Link Avançado' (baixa sem análise) ou aguarde alguns minutos — se continuar ocorrendo muito, será preciso instalar Node.js LTS no Dell e configurar cookies do YouTube"),
        # === NAO TEM FORMATO DISPONIVEL (yt-dlp nao achou video/audio) ===
        # ATENCAO: "unable to download video data" NAO ESTA MAIS AQUI (foi para a regra
        # ESPECIAL acima COMBINADA com HTTP 4xx). Aqui ficam casos que realmente FALTA
        # o formato: unable to EXTRACT video data (antes de baixar), no such format, etc.
        (("no such format", "requested format not available", "format not available",
          "unable to extract video data",
          "could not extract video metadata",
          "extractor failed", "unable to download webpage"),
         "YouTube não forneceu formato compatível (vídeo pode ter sido removido, tornado privado ou o yt-dlp está desatualizado — tente atualizar yt-dlp ou usar o link direto)"),
        # === LOGIN OBRIGATORIO / IDADE / PRIVADO / RESTRITO (o MUITO COMUM hoje em dia) ===
        (("sign in to confirm your age", "age-gate", "age gate", "login required",
          "please sign in", "you need to log in", "authentication required",
          "this video is private", "private video", "members-only", "join this channel",
          "unavailable", "not available in your country", "blocked from the",
          "copyright takedown", "copyright claim", "copyright strike",
          "this video has been removed", "video unavailable", "terminated account"),
         "Vídeo BLOQUEADO: pede login/idade, é privado, só para membros, restrito por país/idade, ou o dono/copyright removeu ele do YouTube"),
        # === HTTP 403 GENERICO (NAO EH RATE LIMIT) — YouTube bloqueou download pq sim ===
        #   (usado SOMENTE SE NAO BATEU a regra especial "unable to download video data:" acima)
        (("http error 403",),
         "YouTube BLOQUEOU o download desse formato de vídeo específico (assinatura de URL expirou, cookies inválidos ou o vídeo tem proteção extra). "
         "Funciona normal no seu navegador Chrome, só o Dell via programa está bloqueado. "
         "Tente usar o 'Link Avançado' ou tente novamente em 1~2 minutos"),
        # === HTTP 429 / RATE LIMIT REAL (só agora — o 403 GENERICO acima pega o resto) ===
        (("http error 429", "too many requests",
          "rate-limited", "you are being rate limited", "quota exceeded"),
         "YouTube bloqueou temporariamente por excesso de downloads (HTTP 429). Aguarde 5~15 min e tente novamente"),
        # === REDE / DNS / TIMEOUT / INTERNET CAIU ===
        (("network error", "temporary failure in name resolution", "dns",
          "network is unreachable", "no route to host", "connection refused",
          "connection reset", "connection timed out", "ssl error", "tls error",
          "timed out", "timeout"),
         "Falha de REDE no servidor Dell: Internet caiu, DNS não resolveu, conexão recusada ou timeout (verifique Wi-Fi / cabo de rede do Dell)"),
        # === STALL / PAROU DE RECEBER DADOS (paramos de receber bytes) ===
        (("stalled for more than", "yt-dlp stalled", "no output", "stopped receiving"),
         "Download parou no meio (não recebeu mais bytes por muito tempo). A internet do Dell ou a conexão do YouTube travou"),
        # === DISCO CHEIO / PERMISSAO / SALVAMENTO ===
        (("no space left on device", "disk full", "storage error",
          "permission denied", "read-only file system", "cannot write", "failed to write"),
         "Não conseguiu SALVAR no disco do Dell: Disco cheio ou sem permissão de escrita na pasta das músicas (/mnt/musicas)"),
        # === LINK INVALIDO / NAO SUPORTADO ===
        (("unsupported url", "unsupported url scheme", "invalid video link",
          "invalid url", "not a valid url", "malformed"),
         "Link INVÁLIDO: não é um link do YouTube válido ou de um site suportado"),
        # === ARQUIVO BAIXADO MAS INVALIDO / CORROMPIDO ===
        (("corrupted or invalid file removed", "integrity check", "failed integrity check",
          "file not found after download", "saved file could not be found",
          "could not find downloaded song", "could not be located"),
         "O download terminou mas o arquivo FINAL ficou corrompido ou desapareceu (falha no HD, espaço insuficiente no final ou download picado)"),
    ]

    @staticmethod
    def _extrair_ultima_linha_de_erro(raw_output: str) -> str:
        """Extrai a ULTIMA linha significativa de erro do stdout/stderr do yt-dlp,
        ignorando linhas de progresso e vazias. Retorna até 300 chars."""
        if not raw_output:
            return ""
        lines = [ln.strip() for ln in raw_output.splitlines() if ln.strip()]
        # Filtra linhas que são claramente progresso % (nao sao erro)
        err_lines = [
            ln for ln in lines
            if not (ln.startswith("[download]") and ("%" in ln or "at" in ln or "speed" in ln))
            and not ln.startswith("[hlsdownload]")
            and not ln.startswith("[downloader]")
            and len(ln) > 10
        ]
        if not err_lines:
            return ""
        last = err_lines[-1][:300]
        # Remove prefixo [extractor] / [youtube] / [generic] etc se houver
        if last.startswith("[") and "]" in last[:20]:
            last = last.split("]", 1)[1].strip()
        return last.strip(": ")

    @classmethod
    def classificar_mensagem_erro(cls, raw_error: str, stalled: bool = False) -> str:
        """[FUNCAO PRINCIPAL] Recebe o erro BRUTO do yt-dlp (texto em ingles/misturado)
        e retorna MENSAGEM 100% PORTUGUES para o USUARIO FINAL VER NA UI
        (aba 'Erros de download'). NUNCA retorna texto generico sem
        explicar o PORQUE. SEMPRE que possivel, anexa uma causa especifica."""
        msg_raw = (raw_error or "").strip()
        if not msg_raw:
            return "Ocorreu um erro desconhecido durante o download (tente novamente)"

        # ==================================================================
        # [V59 GUARDA-CHUVA CRÍTICO: MENSAGEM JA FOI CLASSIFICADA ANTES?
        # Esta funcao pode ser chamada DUAS VEZES no pipeline: (1) worker salva no
        # array errors, (2) routes/queue endpoint reclassifica ao enviar JSON.
        # Se a msg RAW JA COMEÇAR com um dos PREFIXOS conhecidos das categorias
        # PT-BR abaixo → SIGNIFICA QUE JA FOI CLASSIFICADA. Retorna ela
        # DIRETO (nao tenta casar regras de novo — se tentar vai cair
        # em fallback generico porque as regras buscam texto de erro ingles
        # e a mensagem agora comeca em PT-BR sem tokens das regras).
        # ==================================================================
        PREFIXOS_CLASSIFICADOS = (
            "música já existia", "musica ja existia",
            "ffmpeg não está instalado", "ffmpeg nao esta instalado",
            "erro interno de configuração", "erro interno de configuracao",
            "servidor dell falta", "servidor dell:",
            "youtube bloqueou o servidor dell",     # <-- NOVO (regra especial prioridade maxima)
            "esse vídeo funciona normal",            # <-- NOVO fallback
            "youtube detectou bot/captcha", "youtube detectou bot",
            "youtube não forneceu formato", "youtube nao forneceu formato",
            "vídeo bloqueado", "video bloqueado",
            "youtube bloqueou o download desse formato",
            "youtube bloqueou temporariamente por excesso",
            "youtube bloqueou temporariamente (http 429)",
            "falha de rede no servidor dell",
            "download parou de receber",
            "download cancelado: parou de receber",
            "não conseguiu salvar no disco do dell",
            "nao conseguiu salvar no disco do dell",
            "link inválido", "link invalido",
            "o download terminou mas o arquivo final",
            "download falhou (causa especifica",
            "download falhou (causa específica",
        )
        low_start = msg_raw.casefold()
        if any(low_start.startswith(prefixo) for prefixo in PREFIXOS_CLASSIFICADOS):
            # Ja classificada anteriormente: retorna LIMPA e sem duplicata
            return msg_raw.replace("\r", "").replace("\n", " ").strip()

        # Caso stall prioridade maxima (parametro passado pelo caller)
        error_text = msg_raw.casefold()
        last_line = cls._extrair_ultima_linha_de_erro(msg_raw or "")

        # Caso STALL (paramos de receber bytes por timeout) tem prioridade
        if stalled:
            causa_extra = (f" — última linha do yt-dlp: {last_line}" if last_line else "")
            return (
                "Download cancelado: parou de receber dados do YouTube por muito tempo"
                "(internet do Dell caiu, travou ou YouTube bloqueou no meio)"
                + causa_extra
            )

        # Percorre todas as regras em ORDEM (primeira que bate ganha)
        for tokens, mensagem_ptbr in cls.PTBR_ERROR_RULES:
            if any(tok and tok in error_text for tok in tokens):
                # Anexa a ultima linha BRUTA original se tiver algo diferente da msg
                if last_line and last_line.casefold() not in mensagem_ptbr.casefold() and len(last_line) >= 15:
                    return f"{mensagem_ptbr} — Detalhe: {last_line}"
                return mensagem_ptbr

        # === NENHUMA REGRA BATEU (erro desconhecido) ===
        # NÃO retorna frase vazia ou genérica "Falha no download".
        # Mostra algo UTIL: a ULTIMA LINHA do yt-dlp (se existir) porque
        # MESMO EM INGLES, o usuario consegue entender "sign in" / "age" / etc.
        if last_line:
            return (
                "Download falhou (causa específica não identificada) — "
                f"mensagem original do yt-dlp: {last_line}"
            )
        return (
            "Download falhou (não foi possível extrair detalhes da falha) — "
            "tente novamente mais tarde ou cole o link DIRETO no campo 'Link Avançado' "
            "(ele baixa sem análise, só fazendo o download direto)"
        )

    def __init__(
        self,
        events: EventSystem,
        preferences: PreferenceManager,
        song_manager: SongManager,
        queue_manager: QueueManager,
        download_path: str,
        youtubedl_proxy: str | None = None,
        additional_ytdl_args: str | None = None,
    ) -> None:
        """Initialize the download manager.

        Args:
            events: Event system for notifications and broadcasts.
            preferences: Configuration manager for persistent settings.
            song_manager: Manager for song library operations.
            queue_manager: Manager for playback queue.
            download_path: Directory where downloads are saved.
            youtubedl_proxy: Optional proxy URL for yt-dlp.
            additional_ytdl_args: Optional additional arguments for yt-dlp.
        """
        self._events = events
        self._preferences = preferences
        self._song_manager = song_manager
        self._queue_manager = queue_manager
        self._download_path = download_path
        self._youtubedl_proxy = youtubedl_proxy
        self._additional_ytdl_args = additional_ytdl_args
        self.download_queue: Queue = Queue()
        self.pending_downloads: list[dict] = []  # Shadow queue for visibility
        self.download_errors: list[dict] = []  # Track failed downloads
        self.active_download: dict | None = None
        self._worker_thread: Thread | None = None
        self._is_downloading: bool = False  # Track if a download is currently in progress
        self._download_stall_timeout: int = 600  # [V-DOWNLOAD-UNLOCK] Antes 180s = 3min, agora 600s = 10min. NUNCA MAIS aborta download por demora de buffering do YouTube (arquivos grandes).

    def _emitir_notificacao_simples(self, msg: str, tipo: str = "info") -> None:
        """Emite notificacao UNICA para o frontend (SocketIO/Events).
        Remove duplicatas e sempre sincroniza com a UI (evita bug
        'apareceu erro mas depois baixou', ou vice-versa).
        tipos validos: info | success | warning | danger
        """
        tipo = (tipo or "info").lower().strip()
        if tipo not in ("info", "success", "warning", "danger"):
            tipo = "info"
        self._events.emit("notification", msg, tipo)

    def _add_erro_permanente(self, titulo: str, url: str, user: str, msg_ptbr: str) -> None:
        """Adiciona ERRO PERMANENTE (aparece na aba 'Erros' do frontend).
        SÓ CHAMAR NO FINAL DE TUDO, DEPOIS QUE ACABARAM OS RETRIES E
        O DOWNLOAD REALMENTE FALHOU.
        - NÃO usar para avisos transitórios (warn/info de fila,
          não é karaokê, duplicata).
        - Usar SOMENTE para falha definitiva após todas as tentativas.
        """
        self.download_errors.append(
            {
                "id": str(uuid.uuid4()),
                "title": str(titulo or "").strip()[:240],
                "url": str(url or "").strip()[:500],
                "user": str(user or "").strip()[:80],
                "error": str(msg_ptbr or "").strip()[:500],
            }
        )

    def start(self) -> None:
        """Start the download worker thread."""
        self._worker_thread = Thread(target=self._process_queue, daemon=True)
        self._worker_thread.start()
        logging.debug("Download queue worker started")

    def get_downloads_status(self) -> dict:
        """Get the status of active and pending downloads.

        Returns:
            Dict containing 'active' download info and list of 'pending' downloads.
        """
        return {
            "active": self.active_download,
            "pending": self.pending_downloads,
            "errors": self.download_errors,
        }

    def remove_error(self, error_id: str) -> bool:
        """Remove an error from the list by ID.

        Args:
            error_id: The ID of the error to remove.

        Returns:
            True if removed, False if not found.
        """
        initial_len = len(self.download_errors)
        self.download_errors = [e for e in self.download_errors if e["id"] != error_id]
        return len(self.download_errors) < initial_len

    def _normalized_song_key(self, name: str) -> str:
        tidied = regex_tidy(name) or name
        return normalize_for_comparison(tidied)

    def _find_existing_library_match(self, video_url: str, title: str | None) -> str | None:
        video_id = get_youtube_id_from_url(video_url)
        if video_id:
            existing_by_id = self._song_manager.songs.find_by_id(self._download_path, video_id)
            if existing_by_id:
                return existing_by_id

        if not title:
            return None

        wanted_key = self._normalized_song_key(title)
        if not wanted_key:
            return None

        for song_path in self._song_manager.songs:
            existing_name = self._song_manager.display_name_from_path(song_path)
            if self._normalized_song_key(existing_name) == wanted_key:
                return song_path

        return None

    def queue_download(
        self,
        video_url: str,
        enqueue: bool = False,
        user: str = "Pikaraoke",
        title: str | None = None,
        force_skip_analysis: bool = False,
    ) -> tuple[str, str]:
        """Queue a video for download.

        [V52 - REGRAS NOVAS DO USUARIO (14/08/2026) + V52 UI FIX]
        1) force_skip_analysis = True  -> LINK DIRETO (usuario colou a URL na mao).
           BAIXA INCONDICIONALMENTE, SEM ANALISE NENHUMA:
           - Nao verifica se e karaoke.
           - Nao verifica titulo/canal.
           - Somente verifica duplicata por ID (evita baixar 2x a mesma URL).
        2) force_skip_analysis = False -> veio da BUSCA (clicou no resultado da busca).
           BAIXA SOMENTE SE FOR KARAOKE (is_likely_karaoke_result obrigatorio).
        3) Mensagens de ERRO: SEMPRE 100% PT-BR, sem texto em ingles.

        [RETORNO - V52 UI FIX (para ligar botao Search em texto claro)]
        Retorna tupla (status_str, mensagem_ptbr), onde status_str pode ser:
          - "queued"      : adicionado na fila de download OK (vai baixar).
          - "duplicate"   : musica JA EXISTIA na biblioteca (nao baixou de novo).
          - "not_karaoke" : resultado da busca NAO E KARAOKE (bloqueado).
          - "invalid_url" : URL vazia ou completamente invalida.

        Args:
            video_url: YouTube video URL.
            enqueue: Whether to add to playback queue after download.
            user: Username to attribute the download to.
            title: Display title (defaults to URL if not provided).
            force_skip_analysis: V52 flag. True = link direto (baixa SEM checagens).
        """
        from flask_babel import _
        if not video_url or not str(video_url).strip():
            msg_ptbr = "URL inválida: nenhum link foi informado para baixar."
            self._emitir_notificacao_simples(msg_ptbr, tipo="danger")
            return ("invalid_url", msg_ptbr)

        # [V52 HOTFIX URL DUPLICADA - APLICAR NO TOPO SEMPRE]
        try:
            _safe = normalize_youtube_url_to_std(video_url)
            if _safe:
                video_url = _safe
        except Exception:
            pass

        # Strip playlist parameter to avoid downloading entire playlists
        if "&list=" in video_url:
            video_url = video_url.split("&list=")[0]

        fetched_metadata = None
        if not title:
            fetched_metadata = get_video_metadata(video_url)
            title = fetched_metadata.get("title") if fetched_metadata else title

        displayed_title = title if title else video_url

        # [V52 - REGRA #1: VERIFICA DUPLICATA (SEMPRE, MESMO LINK DIRETO)]
        existing_match = self._find_existing_library_match(video_url, title)
        if existing_match:
            existing_name = self._song_manager.display_name_from_path(existing_match)
            logging.info("Rejected duplicate download already in library: %s", displayed_title)
            msg_ptbr = f"Arquivo já existente: {existing_name}"
            self._emitir_notificacao_simples(msg_ptbr, tipo="warning")
            return ("duplicate", msg_ptbr)

        # [V52 - REGRA #2: LINK DIRETO? -> PULA ANALISE.]
        if not force_skip_analysis:
            # ---- VEIO DA BUSCA -> ANALISA SE E KARAOKE OBRIGATORIAMENTE ----
            karaoke_channel = fetched_metadata.get("channel", "") if fetched_metadata else ""
            if title and not is_likely_karaoke_result(title, karaoke_channel):
                logging.warning("Rejected non-karaoke candidate before download: %s", displayed_title)
                msg_ptbr = (
                    "Esta música não é karaokê (o título ou canal não indica versão karaokê. "
                    "Use a busca karaokê ou adicione o link direto SEM análise)."
                )
                self._emitir_notificacao_simples(msg_ptbr, tipo="warning")
                return ("not_karaoke", msg_ptbr)
        else:
            logging.info(
                "[V52 LINK DIRETO] force_skip_analysis=True. Baixando INCONDICIONALMENTE "
                "sem analise de karaoke/idade/restricao: %s", displayed_title
            )

        # Check how many items are ahead (in queue + currently downloading)
        pending_count = self.download_queue.qsize() + (1 if self._is_downloading else 0)

        if pending_count > 0:
            self._emitir_notificacao_simples(
                f"Download aguardando na fila (#{pending_count + 1}): {displayed_title}",
                tipo="info",
            )
        else:
            self._emitir_notificacao_simples(
                f"Download iniciado: {displayed_title}", tipo="info"
            )

        if not self._is_downloading and self.download_queue.empty():
            self._events.emit("download_started")

        download_data = {
            "video_url": video_url,
            "enqueue": enqueue,
            "user": user,
            "title": title,
            "display_title": displayed_title,
        }

        self.download_queue.put(download_data)
        self.pending_downloads.append(download_data)

        return ("queued", download_data["display_title"] or "OK")

    def _process_queue(self) -> None:
        """Worker thread that processes downloads from the queue serially.

        Runs indefinitely, blocking on queue.get() until items are available.
        Each download is processed completely before the next one starts.
        """
        while True:
            download_request = self.download_queue.get()

            # Remove from shadow queue
            # Note: Since this is a single worker thread and append happens on main thread,
            # we simply pop the first item as it corresponds to FIFO queue.
            # In a multi-worker scenario, this would need a lock.
            if self.pending_downloads:
                self.pending_downloads.pop(0)

            self._is_downloading = True

            # Initialize active download state
            self.active_download = {
                "title": download_request.get("display_title", download_request["video_url"]),
                "url": download_request["video_url"],
                "user": download_request["user"],
                "progress": 0.0,
                "status": "starting",
                "eta": "--:--",
                "speed": "---",
            }

            try:
                self._execute_download(
                    download_request["video_url"],
                    download_request["enqueue"],
                    download_request["user"],
                    download_request["title"],
                )
            except Exception as e:
                logging.error(f"Error processing download: {e}")
            finally:
                self._is_downloading = False
                self.active_download = None
                self.download_queue.task_done()

                # Check if we are done with all downloads
                if self.download_queue.empty():
                    self._events.emit("download_stopped")

    def _execute_download(
        self,
        video_url: str,
        enqueue: bool,
        user: str,
        title: str | None,
    ) -> int:
        """Execute a video download.

        Args:
            video_url: YouTube video URL.
            enqueue: Whether to add to queue after download.
            user: Username to attribute the download to.
            title: Display title (defaults to URL if not provided).

        Returns:
            Return code from the download process (0 = success).
        """
        from flask_babel import _

        displayed_title = title if title else video_url

        # MSG: Message shown when download actually starts (after waiting in queue)
        self._events.emit("notification", _("Downloading video: %s") % displayed_title)

        cmd = build_ytdl_download_command(
            video_url,
            self._download_path,
            self._preferences.get_or_default("high_quality"),
            self._youtubedl_proxy,
            self._additional_ytdl_args,
        )
        logging.debug("yt-dlp command: " + " ".join(cmd))

        # Use Popen to capture output in real-time
        process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,  # Line buffered
            universal_newlines=True,
        )

        output_buffer = []
        output_queue: Queue[str | None] = Queue()

        # Regex to parse progress from yt-dlp stdout
        # Example: [download]   0.0% of    4.62MiB at  396.66KiB/s ETA 00:12
        progress_regex = re.compile(
            r"\[download\]\s+(\d+\.?\d*)%\s+of\s+.*?\s+at\s+([^\s]+)\s+ETA\s+([^\s]+)"
        )
        video_id = get_youtube_id_from_url(video_url)
        last_output_at = time.monotonic()
        terminated_for_stall = False

        def _read_stdout() -> None:
            if process.stdout is None:
                output_queue.put(None)
                return
            try:
                for line in iter(process.stdout.readline, ""):
                    if not line:
                        break
                    output_queue.put(line)
            finally:
                output_queue.put(None)

        Thread(target=_read_stdout, daemon=True).start()

        while True:
            try:
                line = output_queue.get(timeout=1)
            except Empty:
                if process.poll() is not None:
                    break
                if time.monotonic() - last_output_at > self._download_stall_timeout:
                    terminated_for_stall = True
                    logging.error(
                        "yt-dlp stalled for over %ss while downloading %s; terminating process",
                        self._download_stall_timeout,
                        displayed_title,
                    )
                    process.kill()
                    break
                continue

            if line is None:
                if process.poll() is not None:
                    break
                continue

            last_output_at = time.monotonic()
            output_buffer.append(line)
            match = progress_regex.search(line)
            if match and self.active_download:
                percent = float(match.group(1))
                speed = match.group(2)
                eta = match.group(3)

                self.active_download["progress"] = percent
                self.active_download["status"] = "downloading"
                self.active_download["speed"] = speed
                self.active_download["eta"] = eta
            elif self.active_download and (
                "[Merger]" in line or "Destination:" in line or "Post-process" in line
            ):
                # yt-dlp sometimes pauses progress updates while finalizing the file.
                self.active_download["status"] = "processing"
                self.active_download["eta"] = "--:--"
            # Log only non-progress lines to avoid spamming logs, or log everything at debug
            # logging.debug(line.strip())

        try:
            rc = process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            rc = process.wait(timeout=5)
        output = "".join(output_buffer)
        if terminated_for_stall:
            output += (
                f"\nyt-dlp stalled for more than {self._download_stall_timeout} seconds "
                "without producing output and was terminated.\n"
            )

        if rc != 0:
            # [V52 - CORRECAO BUG NOTIFICACAO CONFUSA]
            # Antes: em cada falha do retry, ja mostrava notificacao danger
            # e gravava erro permanente mesmo depois de sucesso na tentativa 2/3.
            # Agora:
            #  - WARN em JOURNAL (logging.warning) de cada falha.
            #  - NAO mostra notificacao e NAO grava erro enquanto ainda houver retries.
            #  - SÓ no FINAL (depois que ESGOTOU TODAS as tentativas e rc !=0):
            #       -> 1 notificacao danger (unica) + 1 erro permanente.
            #  - Se acertar em qualquer retry: nao tem notificacao de erro,
            #       cai direto no bloco else (rc==0) com notificacao success VERDE.
            _max_intentos = 3
            _sleep_por_tentativa = [1, 2, 4]  # backoff exponencial curto
            _tentou = 1  # ja tentamos 1 vez no loop anterior
            _output_ultimo = output
            _rc_ultimo = rc
            _stalled_ultimo = terminated_for_stall
            while _tentou < _max_intentos and _rc_ultimo != 0:
                _sleep_seg = _sleep_por_tentativa[(_tentou-1) if (_tentou-1) < len(_sleep_por_tentativa) else -1]
                logging.warning(
                    "[V52 RETRY] Falha tentativa %d/%d rc=%d. Aguardando %ds p/ nova tentativa: %s",
                    _tentou, _max_intentos, _rc_ultimo, _sleep_seg, displayed_title
                )
                # V52: NAO mostramos notificacao em falhas intermediarias
                # (para nao confundir o usuario). So WARN no log.
                time.sleep(_sleep_seg)
                cmd_retry = build_ytdl_download_command(
                    video_url,
                    self._download_path,
                    self._preferences.get_or_default("high_quality"),
                    self._youtubedl_proxy,
                    self._additional_ytdl_args,
                )
                process_r = subprocess.Popen(
                    cmd_retry,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    bufsize=1,
                    universal_newlines=True,
                )
                buf_r = []
                q_r: Queue[str | None] = Queue()
                last_r = time.monotonic()
                stalled_r = False
                def _read_r():
                    if process_r.stdout is None:
                        q_r.put(None); return
                    try:
                        for ln in iter(process_r.stdout.readline, ""):
                            if not ln: break
                            q_r.put(ln)
                    finally:
                        q_r.put(None)
                Thread(target=_read_r, daemon=True).start()
                while True:
                    try: ln = q_r.get(timeout=1)
                    except Empty:
                        if process_r.poll() is not None: break
                        if time.monotonic() - last_r > self._download_stall_timeout:
                            stalled_r = True
                            logging.error(
                                "yt-dlp stalled retry tentativa %d para %s; terminando",
                                _tentou, displayed_title
                            )
                            process_r.kill(); break
                        continue
                    if ln is None:
                        if process_r.poll() is not None: break
                        continue
                    last_r = time.monotonic(); buf_r.append(ln)
                    mr = progress_regex.search(ln)
                    if mr and self.active_download:
                        self.active_download["progress"] = float(mr.group(1))
                        self.active_download["status"]   = "downloading"
                        self.active_download["speed"]    = mr.group(2)
                        self.active_download["eta"]      = mr.group(3)
                try: rc_r = process_r.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process_r.kill(); rc_r = process_r.wait(timeout=5)
                out_r = "".join(buf_r)
                _output_ultimo = out_r
                _rc_ultimo = rc_r
                _stalled_ultimo = stalled_r
                _tentou += 1
                if rc_r == 0:
                    logging.info(
                        "[V52 RETRY SUCESSO] Download OK na tentativa %d/%d: %s",
                        _tentou, _max_intentos, displayed_title
                    )
                    rc = 0
                    output = out_r
                    terminated_for_stall = False
                    break
            # FIM DOS RETRIES.
            # Se rc AINDA for !=0 apos TUDO -> FALHOU DEFINITIVAMENTE: 1 notificacao + 1 erro.
            if rc != 0:
                # [UNIFICADO V53] Usa a FUNCAO PRINCIPAL, com regras completas +
                # extracao da ultima linha de erro real do yt-dlp. Nunca mais
                # mensagem generica "Falha no download: Falha no download".
                user_message = DownloadManager.classificar_mensagem_erro(
                    _output_ultimo, _stalled_ultimo
                )
                combined_raw = f"{user_message} | output_bruto={(_output_ultimo or '')[:600]}"
                # V53: Notificacao DANGER curta mas com a causa REAL, nao generica.
                _causa_curta = (user_message[:160] + "...") if len(user_message) > 160 else user_message
                msg_notif = (
                    f"Falha no download ({_tentou}x tentativas): {displayed_title} — {_causa_curta}"
                )
                self._emitir_notificacao_simples(msg_notif, tipo="danger")
                logging.error(
                    "yt-dlp falhou DEFINITIVO apos %d tentativas para %s | user_msg=%s | raw=%s",
                    _tentou, displayed_title, user_message,
                    combined_raw[:1500]
                )
                # [V53] Erro PERMANENTE (visivel na aba): 100% a msg UTIL.
                self._add_erro_permanente(
                    displayed_title, video_url, user, str(user_message)
                )
        else:
            if self.active_download:
                self.active_download["progress"] = 100
                self.active_download["status"] = "complete"

            if enqueue:
                # V52: SUCESSO verde, 1 vez, sem duplicacao.
                self._emitir_notificacao_simples(
                    f"Baixado e colocado na fila: {displayed_title}", tipo="success"
                )
            else:
                self._emitir_notificacao_simples(
                    f"Baixado: {displayed_title}", tipo="success"
                )

            # After download, find the file path by ID
            song_path = None
            if video_id:
                logging.debug(f"Searching for downloaded file by ID: {video_id}")
                song_path = self._song_manager.songs.find_by_id(self._download_path, video_id)
            else:
                logging.warning("No video ID available to find downloaded song")

            if song_path:
                is_valid, validation_error = validate_media_file(song_path)
                if not is_valid:
                    logging.error(
                        "Downloaded file failed integrity check and will be removed: %s (%s)",
                        song_path,
                        validation_error,
                    )
                    with contextlib.suppress(FileNotFoundError):
                        os.remove(song_path)
                    msg_raw = f"Corrupted or invalid file removed: {validation_error}"
                    msg_ptbr = DownloadManager.classificar_mensagem_erro(msg_raw)
                    self.download_errors.append(
                        {
                            "id": str(uuid.uuid4()),
                            "title": displayed_title,
                            "url": video_url,
                            "user": user,
                            "error": msg_ptbr,
                        }
                    )
                    self._events.emit(
                        "notification",
                        _("Corrupted download removed: %s") % displayed_title,
                        "danger",
                    )
                    song_path = None
                else:
                    suggested_name = self._song_manager.suggest_download_filename(song_path)
                    if suggested_name:
                        current_name = os.path.splitext(os.path.basename(song_path))[0]
                        if suggested_name != current_name:
                            candidate_path = os.path.join(
                                self._download_path,
                                suggested_name + os.path.splitext(song_path)[1],
                            )
                            if not os.path.exists(candidate_path):
                                try:
                                    os.rename(song_path, candidate_path)
                                    song_path = candidate_path
                                    logging.info(
                                        "Renamed downloaded file to normalized title: %s",
                                        os.path.basename(song_path),
                                    )
                                except OSError as e:
                                    logging.warning(
                                        "Could not rename downloaded file %s to %s: %s",
                                        song_path,
                                        candidate_path,
                                        e,
                                    )
                    self._events.emit("song_downloaded", song_path)
            else:
                logging.warning(
                    f"Could not find downloaded song in {self._download_path} matching ID: {video_id}"
                )
                msg_raw = "Download finished but the saved file could not be found"
                msg_ptbr = DownloadManager.classificar_mensagem_erro(msg_raw)
                self.download_errors.append(
                    {
                        "id": str(uuid.uuid4()),
                        "title": displayed_title,
                        "url": video_url,
                        "user": user,
                        "error": msg_ptbr,
                    }
                )
                self._events.emit(
                    "notification",
                    _("Downloaded file could not be located: %s") % displayed_title,
                    "danger",
                )

            if enqueue:
                if song_path:
                    self._queue_manager.enqueue(song_path, user, log_action=False)
                else:
                    # MSG: Message shown after the download is completed but the adding to queue fails
                    self._events.emit(
                        "notification", _("Error queueing song: ") + displayed_title, "danger"
                    )

        return rc
