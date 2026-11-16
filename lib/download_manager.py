"""Download queue manager for serialized video downloads."""

from __future__ import annotations

import contextlib
import json
import logging
import os
import re
import subprocess
import time
import uuid
from queue import Empty, Queue
from threading import Condition, Thread

from pikaraoke.lib.events import EventSystem
from pikaraoke.lib.preference_manager import PreferenceManager
from pikaraoke.lib.queue_manager import QueueManager
from pikaraoke.lib.song_manager import SongManager
from pikaraoke.lib.ffmpeg import validate_media_file
from pikaraoke.lib.get_platform import get_data_directory
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
        pending_downloads: List of pending download requests (single source of
            truth for the FIFO queue, guarded by self._cv).
    """

    # [V99] Pausa PROGRESSIVA quando o YouTube bloqueia o Dell por rate-limit
    # ou bot-check: 1o bloqueio pausa 1h, depois 2h, 4h e 8h (teto). Um download
    # com SUCESSO reseta o nivel para 0. "Tentar agora" (resume_now) limpa a
    # pausa MAS mantem o nivel (o proximo bloqueio continua a escalada).
    PAUSE_STEPS_SECONDS: list[int] = [1 * 3600, 2 * 3600, 4 * 3600, 8 * 3600]
    # Intervalo minimo entre um download e o proximo (espacamento para nao
    # re-triggerar o rate-limit do YouTube).
    DOWNLOAD_GAP_SECONDS: float = 30.0
    # Return code sentinela de _execute_download quando o item foi devolvido
    # para a fila por bloqueio do YouTube (nao conta como falha).
    RC_YOUTUBE_BLOCKED: int = -429
    # Return code sentinela quando um download LENTO foi adiado 5 posicoes.
    RC_DEFERRED: int = -5
    # [V99 LENTO] Adiamento de download lento: so avalia depois de
    # SLOW_CHECK_AFTER_SECONDS; adia se ETA > SLOW_ETA_LIMIT_SECONDS ou se nao
    # chegou a 1% de progresso em SLOW_NO_PROGRESS_SECONDS. Cada item pode ser
    # adiado no maximo MAX_DEFERS vezes (depois baixa ate o fim, evita starvation).
    SLOW_CHECK_AFTER_SECONDS: float = 45.0
    SLOW_ETA_LIMIT_SECONDS: float = 300.0
    SLOW_NO_PROGRESS_SECONDS: float = 120.0
    MAX_DEFERS: int = 2
    QUEUE_STATE_FILENAME = "download_queue_state.json"

    @staticmethod
    def _is_youtube_block(raw_output: str) -> bool:
        """True se a saida do yt-dlp indica BLOQUEIO do Dell pelo YouTube
        (rate-limit global ou bot check), NAO erro de um video especifico.
        Propositalmente NAO inclui 403 generico nem 'unable to download video
        data' (esses sao por-video)."""
        low = (raw_output or "").casefold()
        return any(
            tok in low
            for tok in (
                "rate-limited by youtube",
                "has been rate-limited",
                "http error 429",
                "too many requests",
                "confirm you're not a bot",
                "confirm you’re not a bot",
                "not a bot",
            )
        )

    @staticmethod
    def _parse_eta_seconds(eta: str) -> int | None:
        """Converte ETA do yt-dlp ('00:12', '05:31', '1:02:03') em segundos.
        '--:--', 'Unknown', vazio ou formato invalido retornam None."""
        s = (eta or "").strip()
        if not s or s.casefold() in ("--:--", "unknown"):
            return None
        parts = s.split(":")
        try:
            nums = [int(p) for p in parts]
        except ValueError:
            return None
        if len(nums) == 2:
            return nums[0] * 60 + nums[1]
        if len(nums) == 3:
            return nums[0] * 3600 + nums[1] * 60 + nums[2]
        return None

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
        # [V99] Fila unica: pending_downloads eh a unica fonte de verdade
        # (FIFO). Guardada por _cv; o worker espera nela e tambem acorda
        # quando a pausa termina ou resume_now eh chamado.
        self._cv = Condition()
        self.pending_downloads: list[dict] = []
        self.download_errors: list[dict] = []  # Track failed downloads
        self.active_download: dict | None = None
        self._active_request: dict | None = None  # request em processamento
        # [V99] Estado da pausa por bloqueio do YouTube
        self._paused_until: float = 0.0
        self._pause_reason: str = ""
        self._pause_level: int = 0
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
        """Load persisted queue state, then start the download worker thread."""
        self._load_queue_state()
        self._worker_thread = Thread(target=self._process_queue, daemon=True)
        self._worker_thread.start()
        logging.debug("Download queue worker started")

    def get_downloads_status(self) -> dict:
        """Get the status of active and pending downloads.

        Returns:
            Dict containing 'active' download info and list of 'pending' downloads.
        """
        paused = self.is_paused()
        return {
            "active": self.active_download,
            "pending": self.pending_downloads,
            "errors": self.download_errors,
            "paused": paused,
            "paused_until": self._paused_until if paused else 0.0,
            "pause_reason": self._pause_reason,
            "pause_level": self._pause_level,
        }

    # ============================================================
    # [V99] PAUSA POR BLOQUEIO DO YOUTUBE + PERSISTENCIA DA FILA
    # ============================================================

    def is_paused(self) -> bool:
        """True se os downloads estao pausados por bloqueio do YouTube."""
        return time.time() < self._paused_until

    def _queue_state_path(self) -> str:
        return os.path.join(get_data_directory(), self.QUEUE_STATE_FILENAME)

    def _persist_queue_state(self) -> None:
        """Grava a fila + estado de pausa em JSON de forma atomica (tmp + replace).
        Inclui o request ATIVO no inicio de 'pending' para que um restart no
        meio de um download nao perca o item."""
        try:
            items = []
            if self._active_request:
                items.append(self._active_request)
            # itens re-enfileirados (bloqueio/lentidao) podem ser o MESMO
            # objeto do _active_request: dedup por identidade
            items.extend(
                it for it in self.pending_downloads if it is not self._active_request
            )
            state = {
                "pending": items,
                "paused_until": self._paused_until,
                "pause_reason": self._pause_reason,
                "pause_level": self._pause_level,
            }
            path = self._queue_state_path()
            tmp_path = path + ".tmp"
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(state, f, ensure_ascii=False)
            os.replace(tmp_path, path)
        except Exception as e:
            logging.warning("[V99] falha ao persistir fila de downloads: %s", e)

    def _load_queue_state(self) -> None:
        """Restaura fila e pausa do JSON persistido (chamado em start())."""
        path = self._queue_state_path()
        try:
            with open(path, encoding="utf-8") as f:
                state = json.load(f)
        except FileNotFoundError:
            return
        except Exception as e:
            logging.warning("[V99] arquivo de fila invalido, ignorando: %s", e)
            return
        try:
            self._paused_until = float(state.get("paused_until") or 0.0)
            if self._paused_until <= time.time():
                self._paused_until = 0.0
            self._pause_reason = str(state.get("pause_reason") or "")
            self._pause_level = int(state.get("pause_level") or 0)
        except Exception:
            self._paused_until, self._pause_reason, self._pause_level = 0.0, "", 0
        seen: set[str] = set()
        loaded: list[dict] = []
        for item in state.get("pending") or []:
            url = (item or {}).get("video_url")
            if not url or url in seen:
                continue
            seen.add(url)
            try:
                if self._find_existing_library_match(url, item.get("title")):
                    logging.info("[V99] ignorando item persistido ja na biblioteca: %s", url)
                    continue
            except Exception:
                pass
            loaded.append(item)
        if loaded:
            self.pending_downloads = loaded
            logging.info("[V99] restaurados %d downloads pendentes do disco", len(loaded))
        if self.is_paused():
            logging.info(
                "[V99] pausa de downloads ainda ativa ate %s",
                time.strftime("%H:%M", time.localtime(self._paused_until)),
            )

    def pause_for_block(self, reason: str, at: float | None = None) -> None:
        """Pausa TODOS os downloads por bloqueio do YouTube (progressivo:
        1h -> 2h -> 4h -> 8h). So estende a pausa, nunca encurta."""
        now = time.time() if at is None else at
        duration = self.PAUSE_STEPS_SECONDS[
            min(self._pause_level, len(self.PAUSE_STEPS_SECONDS) - 1)
        ]
        self._pause_level += 1
        with self._cv:
            self._paused_until = max(self._paused_until, now + duration)
            self._pause_reason = (reason or "")[:300]
            self._cv.notify_all()
        resume_hhmm = time.strftime("%H:%M", time.localtime(self._paused_until))
        logging.warning(
            "[V99] YouTube bloqueou o Dell (nivel %d). Downloads pausados por %dh ate %s. Motivo: %s",
            self._pause_level,
            duration // 3600,
            resume_hhmm,
            self._pause_reason,
        )
        self._emitir_notificacao_simples(
            f"Downloads PAUSADOS por {duration // 3600}h: o YouTube bloqueou "
            "temporariamente por excesso de downloads. "
            f"Voltam sozinhos às {resume_hhmm}.",
            tipo="danger",
        )
        self._persist_queue_state()

    def resume_now(self) -> None:
        """'Tentar agora': limpa a pausa atual e acorda o worker.
        NAO reseta o _pause_level (o proximo bloqueio continua a escalada)."""
        with self._cv:
            self._paused_until = 0.0
            self._pause_reason = ""
            self._cv.notify_all()
        logging.info("[V99] pausa de downloads cancelada manualmente (resume_now)")
        self._persist_queue_state()

    def _on_download_success(self) -> None:
        """Um download completou com sucesso: reseta o nivel progressivo."""
        if self._pause_level:
            logging.info("[V99] download com sucesso: reset do nivel de pausa (era %d)", self._pause_level)
            self._pause_level = 0

    def restore_requests(self, items: list[dict], paused_until: float | None = None) -> int:
        """Re-importa requests de download (ex.: snapshot dos ~170 bloqueados).
        Dedup por video_url contra fila+ativo e pula o que ja esta na biblioteca.
        Remove de download_errors os erros cujas URLs foram restauradas.
        Retorna quantos itens foram adicionados."""
        added = 0
        restored_urls: set[str] = set()
        with self._cv:
            existing_urls = {d.get("video_url") for d in self.pending_downloads}
            if self._active_request:
                existing_urls.add(self._active_request.get("video_url"))
            for item in items or []:
                url = (item or {}).get("video_url")
                if not url or url in existing_urls:
                    continue
                try:
                    if self._find_existing_library_match(url, item.get("title")):
                        continue
                except Exception:
                    pass
                req = {
                    "video_url": url,
                    "enqueue": bool(item.get("enqueue")),
                    "user": item.get("user") or "Pikaraoke",
                    "title": item.get("title"),
                    "display_title": item.get("display_title") or item.get("title") or url,
                    "defer_count": int(item.get("defer_count") or 0),
                }
                self.pending_downloads.append(req)
                existing_urls.add(url)
                restored_urls.add(url)
                added += 1
            if paused_until:
                self._paused_until = max(self._paused_until, float(paused_until))
            self._cv.notify_all()
        if restored_urls:
            self.download_errors = [
                e for e in self.download_errors if e.get("url") not in restored_urls
            ]
        if added or paused_until:
            self._persist_queue_state()
        return added

    def _should_defer_slow(
        self, started_at: float, eta_s: int | None, pct: float, request: dict | None
    ) -> bool:
        """[V99 LENTO] True se o download em andamento esta lento demais e deve
        ser adiado 5 posicoes na fila. So adia se: passou SLOW_CHECK_AFTER_SECONDS
        desde o inicio do processo, defer_count < MAX_DEFERS e existe outro item
        esperando na fila."""
        if time.monotonic() - started_at < self.SLOW_CHECK_AFTER_SECONDS:
            return False
        if int((request or {}).get("defer_count") or 0) >= self.MAX_DEFERS:
            return False
        if not self.pending_downloads:
            return False
        if eta_s is not None and eta_s > self.SLOW_ETA_LIMIT_SECONDS:
            return True
        if (
            time.monotonic() - started_at >= self.SLOW_NO_PROGRESS_SECONDS
            and pct < 1.0
        ):
            return True
        return False

    def _defer_slow_download(
        self, request: dict, process, displayed_title: str
    ) -> int:
        """[V99 LENTO] Mata o yt-dlp atual e reinsere o request 5 posicoes
        abaixo na fila para dar preferencia as proximas musicas. O .part fica
        no disco (yt-dlp roda com --continue) e a proxima tentativa retoma."""
        try:
            process.kill()
            process.wait(timeout=5)
        except Exception:
            pass
        request["defer_count"] = int(request.get("defer_count") or 0) + 1
        with self._cv:
            idx = min(5, len(self.pending_downloads))
            self.pending_downloads.insert(idx, request)
        self._persist_queue_state()
        logging.info(
            "[V99 LENTO] %s adiada 5 posicoes (%d/%d)",
            displayed_title,
            request["defer_count"],
            self.MAX_DEFERS,
        )
        self._emitir_notificacao_simples(
            f"Download lento, adiado 5 posições para baixar outras antes: {displayed_title}",
            tipo="info",
        )
        return self.RC_DEFERRED

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
        with self._cv:
            pending_count = len(self.pending_downloads) + (
                1 if self._is_downloading else 0
            )
            paused = self.is_paused()
            paused_until = self._paused_until

        if paused:
            resume_hhmm = time.strftime("%H:%M", time.localtime(paused_until))
            self._emitir_notificacao_simples(
                f"Download na fila (downloads pausados até {resume_hhmm}): {displayed_title}",
                tipo="info",
            )
        elif pending_count > 0:
            self._emitir_notificacao_simples(
                f"Download aguardando na fila (#{pending_count + 1}): {displayed_title}",
                tipo="info",
            )
        else:
            self._emitir_notificacao_simples(
                f"Download iniciado: {displayed_title}", tipo="info"
            )

        if not self._is_downloading and not self.pending_downloads:
            self._events.emit("download_started")

        download_data = {
            "video_url": video_url,
            "enqueue": enqueue,
            "user": user,
            "title": title,
            "display_title": displayed_title,
        }

        with self._cv:
            self.pending_downloads.append(download_data)
            self._cv.notify()
        self._persist_queue_state()

        return ("queued", download_data["display_title"] or "OK")

    def _process_queue(self) -> None:
        """Worker thread that processes downloads from the queue serially.

        Runs indefinitely, waiting on self._cv until items are available and
        downloads are not paused. Each download is processed completely before
        the next one starts, with DOWNLOAD_GAP_SECONDS of spacing between them.
        """
        while True:
            with self._cv:
                while not self.pending_downloads or self.is_paused():
                    wait_s = 60.0
                    if self.is_paused():
                        wait_s = min(60.0, max(1.0, self._paused_until - time.time()))
                    self._cv.wait(timeout=wait_s)
                download_request = self.pending_downloads.pop(0)
                self._active_request = download_request
                self._is_downloading = True
            self._persist_queue_state()

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

            rc = 1
            try:
                rc = self._execute_download(
                    download_request["video_url"],
                    download_request["enqueue"],
                    download_request["user"],
                    download_request["title"],
                    download_request,
                )
            except Exception as e:
                logging.error(f"Error processing download: {e}")
            finally:
                self._is_downloading = False
                self.active_download = None
                self._active_request = None

                # Check if we are done with all downloads
                if not self.pending_downloads:
                    self._events.emit("download_stopped")

            # [V99] Sucesso reseta o nivel progressivo de pausa.
            # rc == RC_YOUTUBE_BLOCKED: item ja foi devolvido ao inicio da fila
            # e a pausa foi aplicada — nao conta como falha nem como sucesso.
            if rc == 0:
                self._on_download_success()
            self._persist_queue_state()

            # [V99] Espacamento entre downloads para nao re-triggerar o
            # rate-limit do YouTube. Interrompivel via cv (resume/novo item).
            # Sem gap apos um adiamento por lentidao: vai direto p/ o proximo.
            if rc != self.RC_DEFERRED:
                with self._cv:
                    self._cv.wait(timeout=self.DOWNLOAD_GAP_SECONDS)

    def _execute_download(
        self,
        video_url: str,
        enqueue: bool,
        user: str,
        title: str | None,
        request: dict | None = None,
    ) -> int:
        """Execute a video download.

        Args:
            video_url: YouTube video URL.
            enqueue: Whether to add to queue after download.
            user: Username to attribute the download to.
            title: Display title (defaults to URL if not provided).
            request: The queue request dict (mutated on defer: defer_count).

        Returns:
            Return code from the download process (0 = success,
            RC_YOUTUBE_BLOCKED = devolvido p/ fila por bloqueio,
            RC_DEFERRED = adiado 5 posicoes por lentidao).
        """
        from flask_babel import _

        displayed_title = title if title else video_url
        if request is None:
            request = {
                "video_url": video_url,
                "enqueue": enqueue,
                "user": user,
                "title": title,
                "display_title": displayed_title,
                "defer_count": 0,
            }

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
        # [V99 LENTO] rastreio de lentidao do download em andamento
        proc_started_at = time.monotonic()
        last_eta_s: int | None = None
        last_pct: float = 0.0

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
            # [V99 LENTO] download lento demais e ha outros esperando -> adia
            if self._should_defer_slow(proc_started_at, last_eta_s, last_pct, request):
                return self._defer_slow_download(request, process, displayed_title)
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
                last_pct = percent
                parsed_eta = self._parse_eta_seconds(eta)
                if parsed_eta is not None:
                    last_eta_s = parsed_eta

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
            # [V99] BLOQUEIO DO YOUTUBE na 1a tentativa? Devolve o item para o
            # inicio da fila, pausa tudo e sai SEM retry/erro permanente.
            if self._is_youtube_block(output):
                return self._handle_youtube_block(
                    video_url, enqueue, user, title, output, request
                )
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
                # [V99 LENTO] mesma regra de adiamento vale nas tentativas de retry
                started_r = time.monotonic()
                eta_r: int | None = None
                pct_r: float = 0.0
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
                    if self._should_defer_slow(started_r, eta_r, pct_r, request):
                        return self._defer_slow_download(request, process_r, displayed_title)
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
                        pct_r = float(mr.group(1))
                        eta_r_parsed = self._parse_eta_seconds(mr.group(3))
                        if eta_r_parsed is not None:
                            eta_r = eta_r_parsed
                try: rc_r = process_r.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process_r.kill(); rc_r = process_r.wait(timeout=5)
                out_r = "".join(buf_r)
                _output_ultimo = out_r
                _rc_ultimo = rc_r
                _stalled_ultimo = stalled_r
                _tentou += 1
                # [V99] BLOQUEIO DO YOUTUBE no retry? Mesma saida: fila + pausa.
                if self._is_youtube_block(out_r):
                    return self._handle_youtube_block(
                        video_url, enqueue, user, title, out_r, request
                    )
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

    def _handle_youtube_block(
        self,
        video_url: str,
        enqueue: bool,
        user: str,
        title: str | None,
        raw_output: str,
        request: dict | None = None,
    ) -> int:
        """[V99] O YouTube bloqueou o Dell (rate-limit/bot check): devolve o
        request para o INICIO da fila (nao perde o item, nao vira erro
        permanente) e aplica a pausa progressiva. Retorna RC_YOUTUBE_BLOCKED."""
        reason = (
            self._extrair_ultima_linha_de_erro(raw_output)
            or "YouTube rate limit / bot check"
        )
        if request is None:
            request = {
                "video_url": video_url,
                "enqueue": enqueue,
                "user": user,
                "title": title,
                "display_title": title if title else video_url,
            }
        with self._cv:
            self.pending_downloads.insert(0, request)
        logging.warning(
            "[V99] bloqueio do YouTube detectado; item devolvido ao inicio da fila: %s",
            request["display_title"],
        )
        self.pause_for_block(reason)
        return self.RC_YOUTUBE_BLOCKED
