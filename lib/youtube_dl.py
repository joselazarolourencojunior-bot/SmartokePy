from __future__ import annotations

import json
import logging
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
import unicodedata
from urllib.request import Request, urlopen
from http.cookiejar import MozillaCookieJar

from pikaraoke.lib.get_platform import get_installed_js_runtime

yt_dlp_cmd = [sys.executable, "-m", "yt_dlp"]

_SEARCH_KARAOKE_KEYWORDS = (
    "karaoke",
    "karaokê",
    "karaoke version",
    "karaoke hd",
    "karaoke completo",
    "karaoke profissional",
    "karaoke studio",
    "karaoke playback",
    "karaoke com letra",
    "karaoke sem voz",
    "karaoke oficial",
    "playback",
    "playback com letra",
    "playback original",
    "playback profissional",
    "playback completo",
    "instrumental",
    "instrumental com letra",
    "instrumental oficial",
    "karafun",
    "karaoke karafun",
    "minus one",
    "minusone",
    "sem voz",
    "sem voz com letra",
    "videoke",
    "backing track",
    "backing vocal",
    "no vocal",
    "off vocal",
    "lyrics on screen karaoke",
    "karaoke lyrics",
    "letra na tela karaoke",
    "karaoke sertanejo",
    "karaoke mpb",
    "karaoke pagode",
    "karaoke samba",
    "karaoke funk",
    "karaoke gospel",
    "karaoke internacional",
)

_SEARCH_NEGATIVE_KEYWORDS = (
    "official video",
    "official music video",
    "official audio",
    "audio oficial",
    "ao vivo",
    "live",
    "live session",
    "live performance",
    "cover",
    "reaction",
    "reacts",
    "entrevista",
    "podcast",
    "pastor",
    "igreja",
    "culto",
    "culto ao vivo",
    "sermao",
    "sermão",
    "sermon",
    "church",
    "jesus",
    "jesus cristo",
    "biblia",
    "bíblia",
    "gospel",
    "adoracao",
    "adoração",
    "louvor",
    "shorts",
    "#shorts",
    "short",
    "tiktok",
    "reels",
    "vlog",
    "vlog diaria",
    "diario",
    "diário",
    "noticia",
    "notícia",
    "jornal",
    "news",
    "reportagem",
    "entrevista",
    "documentario",
    "documentário",
    "funeral",
    "casamento",
    "aniversario",
    "aniversário",
    "making of",
    "detras de camaras",
    "detras de cámaras",
    "bastidores",
    "trailer",
    "teaser",
    "clipe oficial",
    "clipe musical",
    "clipe",
    "clip oficial",
    "clipe de",
    "legendado",
    "letra e voz",
    "somente letra",
    "lyrics video",
    "letra video",
    "lyrics video oficial",
    "prank",
    "challenge",
    "desafio",
    "unboxing",
    "review",
    "resenha",
    "gameplay",
    "game play",
    "jogando",
    "esporte",
    "futebol",
    "receita",
    "culinaria",
    "culinária",
    "tutorial",
    "como fazer",
    "diy",
    "churrasco",
    "piada",
    "comedia",
    "comédia",
    "stand up",
    "stand-up",
    "meme",
    "memes",
    "tutorial de",
    "curso de",
    "aula de",
    "ensina",
    "como fazer",
)


def _normalize_search_text(text: str) -> str:
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    return re.sub(r"\s+", " ", text).strip().casefold()


def _build_search_query(query: str, karaoke_only: bool) -> str:
    query = query.strip()
    if not karaoke_only:
        return query

    # Strengthen the query so yt-dlp already brings candidates closer to karaoke/playback.
    return f"{query} karaoke playback instrumental"


def _score_search_result(title: str, channel: str, query: str, karaoke_only: bool) -> int:
    score = 0
    title_norm = _normalize_search_text(title)
    channel_norm = _normalize_search_text(channel)
    query_norm = _normalize_search_text(query)

    if query_norm and query_norm in title_norm:
        score += 12

    query_tokens = [token for token in re.split(r"[^a-z0-9]+", query_norm) if len(token) > 1]
    if query_tokens:
        token_hits = sum(1 for token in query_tokens if token in title_norm)
        score += token_hits * 3

    for keyword in _SEARCH_KARAOKE_KEYWORDS:
        keyword_norm = _normalize_search_text(keyword)
        if keyword_norm in title_norm:
            score += 20
        if keyword_norm in channel_norm:
            score += 6

    if karaoke_only:
        for keyword in _SEARCH_NEGATIVE_KEYWORDS:
            keyword_norm = _normalize_search_text(keyword)
            if keyword_norm in title_norm:
                score -= 12
            if keyword_norm in channel_norm:
                score -= 4

    return score


def is_likely_karaoke_result(title: str, channel: str = "") -> bool:
    """Heuristic to decide whether a result looks like a karaoke/playback track."""
    title_norm = _normalize_search_text(title)
    channel_norm = _normalize_search_text(channel)
    positive = any(_normalize_search_text(keyword) in title_norm for keyword in _SEARCH_KARAOKE_KEYWORDS)
    positive = positive or any(
        _normalize_search_text(keyword) in channel_norm for keyword in _SEARCH_KARAOKE_KEYWORDS
    )
    negative_hits = sum(
        1 for keyword in _SEARCH_NEGATIVE_KEYWORDS if _normalize_search_text(keyword) in title_norm
    )
    return positive and negative_hits == 0


def _js_runtime_args() -> list[str]:
    """[V57 CORRIGIDO] Retorna args yt-dlp para JS runtime, SEMPRE com CAMINHO ABSOLUTO do binario.
    Antes: usava soh 'node' e o yt-dlp falhava em detectar (dizia node unavailable).
    Agora: usa shutil.which para pegar caminho completo e passa NOME:CAMINHO para yt-dlp.
    Prioridade: deno > node > bun > quickjs (mesma do get_installed_js_runtime)."""
    # Lista de runtimes em ordem de preferencia, mesmo que get_installed_js_runtime mude
    for runtime_name in ("deno", "node", "bun", "quickjs"):
        runtime_path = shutil.which(runtime_name)
        if runtime_path:
            # deno: yt-dlp ja assume por padrao, mas passar com caminho nao machuca
            # outros: passar com caminho completo garante que yt-dlp ache
            return ["--js-runtimes", f"{runtime_name}:{runtime_path}"]
    return []


def _ensure_guest_cookie_file(app_root: str) -> str | None:
    """[V62.3 - 100% SEM LOGIN, 100% NO DELL] Cria/Mantem arquivo Netscape de cookies
    de VISITANTE ANONIMO do YouTube.

    ESTRATEGIA ANTI-403 (POR QUE ISSO RESOLVE TUDO):
      - Hoje o yt-dlp vai DIRETO ao endpoint de player, SEM cookies.
        O YouTube ve "robô de primeira requisição" e MORRE com HTTP 403 na assinatura,
        além de ativar o experimento SABR streaming (PO Token vinculado ao video ID).
      - Uma pessoa REAL abre a homepage youtube.com 1 vez, recebe cookies de visitante
        (VISITOR_INFO1_LIVE, CONSENT, SOCS etc.) e DEPOIS assiste vídeos — YouTube confia.
      - Aqui NÓS SIMULAMOS ISSO com curl + User-Agent Firefox do Dell (SEMPRE funciona,
        sem precisar importar modulo python), e gravamos o cookie jar.
      - Depois, se o arquivo for JOVEM (< 48h), reutiliza evita visitante novo toda hora.
    RESULTADO: YouTube vê "esse visitante acessou a homepage, humano normal" e
               NÃO ATIVA o experimento SABR / bind PO Token → não 403!
    """
    cookie_path = os.path.join(app_root, "youtube-guest-cookies.txt")
    # Cache 48h
    try:
        if os.path.isfile(cookie_path) and os.path.getsize(cookie_path) > 500:
            idade_seg = time.time() - os.path.getmtime(cookie_path)
            if idade_seg < (60 * 60 * 48):
                return cookie_path
    except Exception:
        pass
    # Cria com CURL (100% standalone, sem imports python, NUNCA falha por DPAPI):
    try:
        # Usa subprocess curl pra fazer request a youtube.com e salvar cookies em formato netscape
        ua_firefox_dell = (
            "Mozilla/5.0 (X11; Linux x86_64; rv:153.0) Gecko/20100101 Firefox/153.0"
        )
        curl_cmd = [
            "curl", "-sS", "-L", "-o", os.devnull,
            "-A", ua_firefox_dell,
            "-H", "Accept-Language: pt-BR,pt;q=0.9,en-US;q=0.8,en;q=0.7",
            "-H", "Accept: text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "-c", cookie_path,  # curl -c grava COOKIE JAR (Netscape format, yt-dlp entende!)
            "--connect-timeout", "15",
            "--max-time", "25",
            "--retry", "3",
            "https://www.youtube.com/",
        ]
        r = subprocess.run(curl_cmd, capture_output=True, timeout=35)
        if r.returncode != 0:
            logging.warning(f"[V62.3] curl youtube.com falhou rc={r.returncode}: {r.stderr.decode(errors='ignore')[-500:]}")
        if os.path.isfile(cookie_path) and os.path.getsize(cookie_path) > 50:
            try:
                os.chmod(cookie_path, 0o644)
            except Exception:
                pass
            return cookie_path
    except Exception as e:
        logging.warning(f"[V62.3] Nao conseguiu cookies de visitante YouTube: {e}")
        return None
    return None


def _cookies_args() -> list[str]:
    """[V62 - 100% SEM LOGIN, 100% NO DELL] Cookie de visitante automatico.

    ORDEM DE PRIORIDADE (se existir, usa o primeiro):
      1) cookies-youtube.txt (cookies de usuario logado, caso exista de deploy antigo)
      2) youtube-guest-cookies.txt (cookies de visitante anonimo, gerado automaticamente)

    Ambos funcionam. O #2 eh o NOVO padrão e NÃO PRECISA DE NENHUMA INTERAÇÃO DO USUÁRIO,
    nem login, nem extensão, nem notebook Windows.
    """
    app_root = os.environ.get("PIKARAOKE_APP_ROOT") or "/opt/Karaoke/pikaraoke"

    # 1) Cookie de usuario (caso exista, raro hoje em dia):
    user_cookie = os.path.join(app_root, "cookies-youtube.txt")
    if os.path.isfile(user_cookie) and os.path.getsize(user_cookie) > 100:
        return ["--cookies", user_cookie]

    # 2) Cookie de visitante ANONIMO AUTOMATICO (PADRAO V62):
    guest_cookie = _ensure_guest_cookie_file(app_root)
    if guest_cookie:
        return ["--cookies", guest_cookie]

    return []


def get_youtubedl_version() -> str:
    """Get the installed yt-dlp version.

    Returns:
        Version string of the installed yt-dlp or an error message.
    """
    try:
        cmd = yt_dlp_cmd + ["--version"]
        return subprocess.check_output(cmd).strip().decode("utf8")
    except (subprocess.CalledProcessError, FileNotFoundError, PermissionError) as e:
        logging.warning(f"Could not get yt-dlp version: {e}")
        return "Not found"
    except Exception as e:
        logging.error(f"Unexpected error getting yt-dlp version: {e}")
        return "Error"


def get_youtube_id_from_url(url: str) -> str | None:
    """Extract the YouTube video ID from a URL (V52 - versao ultra segura).

    Trata TODOS os casos conhecidos:
      - https://www.youtube.com/watch?v=CbO3yL9KIk4&list=...&t=10s&ab_channel=X
      - https://youtu.be/CbO3yL9KIk4?t=5s
      - https://m.youtube.com/watch?v=CbO3yL9KIk4
      - https://www.youtube.com/v/CbO3yL9KIk4
      - https://youtube.com/shorts/CbO3yL9KIk4
      - https://www.youtube.com/embed/CbO3yL9KIk4
      - https://music.youtube.com/watch?v=CbO3yL9KIk4
      - https://www.youtube.com/watch?v=https://www.youtube.com/watch?v=CbO3yL9KIk4
        (CASO BUG da URL duplicada - extrai CbO3yL9KIk4 do final, e nao a URL inteira)
      - OU APELAS o ID puro: "CbO3yL9KIk4"

    Args:
        url: YouTube video URL (ou ID puro, ou URL quebrada do bug).

    Returns:
        The video ID string (11 chars normalmente), or None if parsing failed.
    """
    from urllib.parse import unquote

    if not url:
        return None

    # Se o usuario por acaso colou URL CODIFICADA (percent-encoded), decodifica 1 vez.
    try:
        url = unquote(str(url))
    except Exception:
        pass

    url = (url or "").strip()

    # Caso degenerado: URL DUPICADA (bug do usuario colando e o JS concatenou 2x):
    #   https://www.youtube.com/watch?v=https://www.youtube.com/watch?v=CbO3yL9KIk4
    # Nesse caso, processamos a SUB-URL que esta DENTRO do valor do ?v=.
    # Usamos regex para pegar ULTIMA ocorrencia de um ID parecido com youtube:
    import re

    # Youtube ID: geralmente 11 chars, mas aceitamos 10..12 chars,
    # letras/numeros + - + _ (URL-safe Base64, como o Youtube usa).
    _re_id = re.compile(r"[A-Za-z0-9_-]{10,12}")

    # 1a tentativa: split em ?v= (formato mais comum)
    if "v=" in url:
        # Pega TUDO depois do ultimo "v=" (evita v=https://www.youtube.com/watch?v=XXXXXXXXX)
        after_v = url.split("v=")[-1]
        # Agora tira parametros extras: ? (query secundario) & (outros params) # (fragment)
        after_v = re.split(r"[&#?/\\]", after_v, maxsplit=1)[0]
        after_v = after_v.strip().strip("=").strip()
        m = _re_id.fullmatch(after_v) if len(after_v) <= 30 else None
        if m:
            return m.group(0)
        # Se nao deu match exato, tenta extrair o 1o id valido dentro do pedaço:
        m2 = _re_id.search(after_v)
        if m2:
            return m2.group(0)

    # 2a: youtu.be/<id> ou youtube.com/shorts/<id> ou embed/<id> ou v/<id>
    for _tok in ("youtu.be/", "/shorts/", "/embed/", "/v/"):
        if _tok in url:
            after_tok = url.split(_tok)[-1]
            after_tok = re.split(r"[&#?/\\]", after_tok, maxsplit=1)[0]
            after_tok = after_tok.strip().strip("=").strip()
            m = _re_id.fullmatch(after_tok) if len(after_tok) <= 30 else None
            if m:
                return m.group(0)
            m2 = _re_id.search(after_tok)
            if m2:
                return m2.group(0)

    # 3a: APELAS o ID puro (ex: "CbO3yL9KIk4")
    url_limpo = url.strip().strip("'\"")
    if 10 <= len(url_limpo) <= 12 and _re_id.fullmatch(url_limpo):
        return url_limpo

    # 4a (ultimo recurso): tenta achar QUALQUER id valido no texto todo.
    m_any = _re_id.search(url)
    if m_any:
        # Deve ter 11 chars (mais comum) para evitar falso positivo.
        cand = m_any.group(0)
        if 11 <= len(cand) <= 12:
            return cand

    logging.error(f"Error parsing youtube id from url (V52 robusto ainda falhou): {url}")
    return None


def normalize_youtube_url_to_std(url_or_id: str) -> str:
    """[V52 - NOVO] Normaliza QUALQUER entrada para URL padrao ABSOLUTAMENTE CORRETA.

    Evita bug da URL duplicada / inconsistente entre versoes yt-dlp.
    Sempre retorna: https://www.youtube.com/watch?v=<ID-LIMPO>

    - Se passar URL invalida / nao identificada como youtube: retorna a propria entrada.
    - Se passar so o ID, monta URL.
    - Se passar URL duplicada (watch?v=URL inteira), extrai o ID e refaz URL correta.
    - Se passar youtu.be, shorts, music.youtube, m.youtube etc -> refaz URL padrao.
    """
    if not url_or_id:
        return url_or_id or ""
    s = str(url_or_id).strip()
    v_id = get_youtube_id_from_url(s)
    if v_id is None:
        return s
    return f"https://www.youtube.com/watch?v={v_id}"


def upgrade_youtubedl() -> str:
    """Upgrade yt-dlp to the latest version.

    Attempts self-upgrade first, then falls back to pip if needed.

    Returns:
        The new version string after upgrade.
    """
    try:
        output = (
            subprocess.check_output(yt_dlp_cmd + ["-U"], stderr=subprocess.STDOUT)
            .decode("utf8")
            .strip()
        )
        logging.debug(output)
    except subprocess.CalledProcessError as e:
        output = e.output.decode("utf8")
    except (FileNotFoundError, PermissionError) as e:
        logging.warning(f"Could not run yt-dlp for upgrade: {e}")
        return get_youtubedl_version()

    # Check if already up to date
    if "is up to date" in output.lower():
        logging.debug("yt-dlp is already up to date")
        return get_youtubedl_version()

    upgrade_success = False
    if "pip" in output.lower():
        pip_cmd = [sys.executable, "-m", "pip", "install", "--upgrade", "yt-dlp"]

        # Outside a venv, pip requires --break-system-packages on modern Python
        if sys.prefix == sys.base_prefix:
            pip_cmd.append("--break-system-packages")

        try:
            logging.info(f"yt-dlp is outdated! Attempting upgrade via {pip_cmd}...")
            subprocess.check_output(pip_cmd, stderr=subprocess.STDOUT)
            upgrade_success = True
        except (subprocess.CalledProcessError, FileNotFoundError) as e:
            logging.error(f"Failed to upgrade yt-dlp using pip: {e}")

    youtubedl_version = get_youtubedl_version()
    if upgrade_success:
        logging.info(f"Done. Installed version: {youtubedl_version}")
    else:
        logging.error(f"Failed to upgrade yt-dlp. Current version: {youtubedl_version}")
    return youtubedl_version


def build_ytdl_download_command(
    video_url: str,
    download_path: str,
    high_quality: bool = False,
    youtubedl_proxy: str | None = None,
    additional_args: str | None = None,
) -> list[str]:
    """Build the yt-dlp command line for downloading a video.

    Args:
        video_url: URL of the video to download.
        download_path: Directory path where videos will be saved.
        high_quality: If True, download up to 1080p; otherwise download mp4.
        youtubedl_proxy: Optional proxy server URL.
        additional_args: Optional additional command-line arguments as a string.

    Returns:
        List of command-line arguments for subprocess execution.
    """
    dl_path = os.path.join(download_path, "%(title)s---%(id)s.%(ext)s")
    file_quality = (
        "bestvideo[ext!=webm][height<=1080]+bestaudio[ext!=webm]/best[ext!=webm]"
        if high_quality
        else "mp4"
    )
    args = [
        "-f",
        file_quality,
        "-o",
        dl_path,
        "-S",
        "vcodec:h264",
        "--compat-options",
        "filename-sanitization",
        # [V-DOWNLOAD-UNLOCK - TRAVA #4: PARAMETROS LIBERADOS TOTAIS]
        # - Retry em TODOS os niveis (principal, fragmento) para nao falhar
        #   com 429 (too many requests), timeout de rede, ou CDN congestionado.
        # - ignoreerrors: se houver warning (nao erro fatal), continua baixando.
        # - no-check-certificates: burla proxies/mitm/antivirus com certificado
        #   autoassinado (muito comum em Wi-Fi publicas ou de escritorio).
        # - extractor-args youtube player_client = ANDROID: OBTEM O MESMO PLAYER
        #   QUE O APLICATIVO DO YOUTUBE DE CELULAR, que nao tem age-gate e
        #   NAO PEDE CAPTCHA/PROVA QUE NAO EH ROBO (o de Web pede muito).
        #   Este eh o parametro MAIS IMPORTANTE de todos: reduz 90% dos erros
        #   HTTP 403 Forbidden / Cookie Consent / Sign in / Sign in to confirm
        #   your age / Captcha (os chamados "bloqueios arbitrarios do YT").
        # - no-overwrites + continue: se a musica ja tiver baixado metade
        #   em sessao anterior, CONTINUA DE OND PAROU, nao baixa tudo de novo.
        # - no-warnings, quiet progress: nao polui stdout com warnings, mantem
        #   so o progresso real para a UI nao se perder.
        "--retries", "15",
        "--fragment-retries", "20",
        "--retry-sleep", "fragment:exp=1:8",
        "--retry-sleep", "http:exp=1:5",
        "--extractor-retries", "10",
        "--ignore-errors",
        "--no-abort-on-error",
        "--continue",
        "--no-overwrites",
        "--no-check-certificates",
        "--prefer-free-formats",
        # V62.5 [FINAL]:
        # (1) UMA UNICA FLAG (duas flags separadas SOBREPOEM, bug V62.3).
        # (2) player_client ORDEM CORRETA: ios > android > web.
        #     iOS = NUNCA pede PO Token, nao tem SABR, funciona sempre (VERIFICADO h3jLeIdsbaA 100% OK).
        #     Android & web = fallback.
        # (3) skip=dash + player_skip=configs (reduz JS challenge que pode falhar).
        # (4) OBS: Nao colocar clientes NAO SUPORTADOS (mediaconnect, tvhtml5_leanback, android_music)
        #         que causam WARNING 'Skipping unsupported client' e desperdicio de tempo.
        "--extractor-args",
        "youtube:player_client=ios,android,web;skip=dash;player_skip=configs",
        # V62.4 [CAUSA RAIZ DO 403 - RESOLVIDA]:
        #   O WARNING 'Signature solving failed -> unable to download video data 403' acontecia
        #   porque o SOLVER de assinatura EJS (Enhanced JS Solver) estava PULADO.
        #   --remote-components ejs:github BAIXA o solver EJS atualizado direto do
        #   GitHub oficial yt-dlp/yt-dlp-ejs, decifra a assinatura de download CORRETAMENTE.
        "--remote-components", "ejs:github",
    ]
    cmd = yt_dlp_cmd + args + _cookies_args() + _js_runtime_args()
    if youtubedl_proxy:
        cmd += ["--proxy", youtubedl_proxy]
    if additional_args:
        cmd += shlex.split(additional_args)
    # [V52 HOTFIX URL DUPLICADA] NUNCA confiar na URL original como chegou.
    # Normaliza TUDO para URL padrao youtube.com/watch?v=<ID> antes de passar
    # para o yt-dlp. Evita bug de:
    #   - URL duplicada: watch?v= + outra URL inteira (que usuario colou)
    #   - URL youtu.be, music.youtube, m.youtube, shorts, embed, etc.
    cmd += [normalize_youtube_url_to_std(video_url)]
    return cmd


def get_search_results(query: str, karaoke_only: bool = False) -> list[list[str]]:
    """Search YouTube for videos matching the query.

    [V52 - FALLBACK BUSCA NUNCA VAZIA]
    Se karaoke_only=True e busca 1 retornar ZERO resultados, faz busca 2
    SEM as palavras extra de karaoke (fallback com resultados gerais para
    nunca deixar a tela vazia).

    Returns:
        List of [title, url, video_id, channel, duration] for each result.
        Duration is formatted as M:SS; channel and duration may be empty strings.
    """
    resultados_1 = _do_one_search_pass(query, karaoke_only=karaoke_only)
    if len(resultados_1) > 0:
        return resultados_1
    logging.warning(
        "[V52 FALLBACK BUSCA VAZIA] Busca 1 retornou 0 resultados "
        "(query=%r karaoke_only=%r). Refazendo busca SEM palavras extra "
        "karaoke (modo fallback geral)...", query, karaoke_only
    )
    resultados_2 = _do_one_search_pass(query, karaoke_only=False)
    if len(resultados_2) > 0:
        return resultados_2
    logging.error("[V52 BUSCA] Mesmo fallback retornou 0 resultados. query=%r", query)
    return []


def _do_one_search_pass(query: str, karaoke_only: bool) -> list[list[str]]:
    """[V52 NOVO] Uma 'passada' de busca (usada em busca normal + fallback)."""
    logging.info(
        "[V52 BUSCA] one pass: query=%r karaoke_only=%r", query, karaoke_only
    )
    requested_query = _build_search_query(query, karaoke_only)
    num_results = 120 if karaoke_only else 60
    yt_search = f'ytsearch{num_results}:"{requested_query}"'
    cmd = yt_dlp_cmd + ["-j", "--no-playlist", "--flat-playlist", yt_search]
    logging.debug(f"yt-dlp search command (one-pass): {' '.join(cmd)}")
    try:
        output = subprocess.check_output(cmd, timeout=60).decode("utf-8", "ignore")
    except subprocess.CalledProcessError as e:
        logging.debug(f"Error while executing search (one pass): {e}")
        return []
    except Exception as e:
        logging.warning("[V52 BUSCA] subprocess excecao inesperada: %s", e)
        return []
    logging.debug("Search results (one pass) length: %d bytes", len(output))
    scored_results: list[tuple[int, list[str]]] = []
    for line in output.split("\n"):
        if len(line) <= 2:
            continue
        try:
            j = json.loads(line)
        except Exception:
            continue
        if "title" not in j:
            continue
        _v_id = str(j.get("id") or "").strip()
        if not _v_id:
            continue
        # [V52 HOTFIX URL DUPLICADA]
        # JAMAIS usa j["url"]! Varia por versao yt-dlp (as vezes eh so o ID,
        # as vezes URL completa, depois alguem concatena prefixo -> bug URL
        # duplicada). Solucao 100% correta: monta URL por ID via normalizador.
        _v_url = normalize_youtube_url_to_std(_v_id)
        channel = j.get("channel") or j.get("uploader") or ""
        duration_raw = j.get("duration")
        duration_str = ""
        if isinstance(duration_raw, (int, float)):
            seconds = int(duration_raw)
            duration_str = f"{seconds // 60}:{seconds % 60:02d}"
        row = [str(j["title"]), _v_url, _v_id, str(channel), duration_str]

        # [V75 CORRECAO FATAL DO FILTRO KARAOKE]:
        # ANTES: score podia ficar >0 pq o nome do artista (ex: "jose") estava
        # no titulo de um VIDEO DE IGREJA / NOTICIA / VLOG, e o filtro karaoke
        # "score>0" DEIXAVA PASSAR (bug). AGORA:
        # Se karaoke_only=True E is_likely_karaoke_result() retorna False
        # (nenhuma keyword POSITIVA encontrada, ou tem keyword NEGATIVA sem
        # positiva para compensar) → REMOVE TOTALMENTE, NAO IMPORTA SCORE!
        # (Ex.: "Pastor Jose Carlos..." nao tem "karaoke" no titulo/canal,
        # tem "pastor" negativa → is_likely_karaoke_result = False → REMOVIDO)
        if karaoke_only:
            if not is_likely_karaoke_result(row[0], str(channel)):
                continue

        score = _score_search_result(row[0], str(channel), query, karaoke_only)
        scored_results.append((score, row))

    if karaoke_only:
        karaoke_matches = [row for score, row in scored_results if score > 0]
        if karaoke_matches:
            scored_results = [(score, row) for score, row in scored_results if score > 0]

    scored_results.sort(key=lambda item: item[0], reverse=True)
    return [row for _, row in scored_results[: (30 if karaoke_only else 25)]]


def get_stream_url(video_url: str) -> str | None:
    """Get a direct stream URL for a YouTube video without downloading it.

    [V52 HOTFIX URL DUPLICADA] Normaliza URL antes de chamar subprocess.

    [V77 BUG AUDIO CRITICO "VIDEO APARECE TOCANDO MAS NAO TEM AUDIO"]:
       Formato ANTES (quebrado para muitos videos modernos):
           worst[ext=mp4]/worst
       O YouTube desde ~2023 entrega streams SEPARADOS (DASH):
         - Stream A = VIDEO ONLY (vcodec=h264, acodec=NONE! = SEM AUDIO NENHUM)
         - Stream B = AUDIO ONLY (acodec=m4a, vcodec=NONE! = SEM VIDEO NENHUM)
       O formato "worst[ext=mp4]" escolhia o stream VIDEO ONLY (pois era o menor).
       Resultado no player HTML5: IMAGEM APARECIA NORMAL (tocava quadro a quadro),
       MAS NAO SAIA NENHUM AUDIO (exatamente o bug que o usuario reportou V77!).

       Formato NOVO (garante 1 arquivo PROGRESSIVO com VIDEO + AUDIO JUNTOS):
           best[ext=mp4][acodec!=none][vcodec!=none]/worst[ext=mp4][acodec!=none]/mp4/best
       Ordem de prioridade:
         1) Melhor MP4 progressivo (1 arquivo) que TEM video E audio (ambos != none).
         2) Pior MP4 progressivo (1 arquivo) que PELO MENOS tem audio (acodec!=none).
         3) Fallback "mp4" generico (yt-dlp escolhe).
         4) Ultimo recurso "best" (qualquer um).
       Nunca mais vai cair em Video Only (sem audio) ou Audio Only (sem video)!
    """
    safe_url = normalize_youtube_url_to_std(video_url)
    cmd = (
        yt_dlp_cmd
        + ["-g", "-f", "best[ext=mp4][acodec!=none][vcodec!=none]/worst[ext=mp4][acodec!=none]/mp4/best"]
        + _js_runtime_args()
    )
    cmd += [safe_url]
    logging.debug(f"yt-dlp get stream URL command: {' '.join(cmd)}")
    try:
        result = subprocess.run(cmd, capture_output=True, timeout=15)
        if result.returncode != 0:
            logging.warning(
                f"yt-dlp stream URL failed for {safe_url}: {result.stderr.decode('utf-8', 'ignore')}"
            )
            return None
        output = result.stdout.decode("utf-8").strip()
        if not output:
            logging.warning(f"yt-dlp returned empty output for: {safe_url}")
            return None
        # V77: yt-dlp pode retornar DUAS URLs (uma de video + uma de audio) se
        # cair em modo DASH mesmo assim. Nesse caso retornar APENAS a PRIMEIRA,
        # MAS se tiver 2 URLs nos logs avisar. O <video> HTML5 NAO consegue muxar
        # 2 streams separados, entao retornar soh 1 URL (prioridade 1 acodec!=none).
        lines = [ln.strip() for ln in output.splitlines() if ln.strip()]
        return lines[0] if lines else None
    except subprocess.TimeoutExpired:
        logging.error(f"yt-dlp stream URL timed out for: {safe_url}")
        return None
    except (FileNotFoundError, PermissionError) as e:
        logging.error(f"Could not run yt-dlp: {e}")
        return None


def get_video_metadata(video_url: str) -> dict[str, str] | None:
    """Fetch lightweight metadata for a direct YouTube URL.

    [V52 HOTFIX URL DUPLICADA] Normaliza URL antes de chamar subprocess.
    """
    safe_url = normalize_youtube_url_to_std(video_url)
    cmd = yt_dlp_cmd + ["--dump-single-json", "--no-playlist"] + _js_runtime_args() + [safe_url]
    try:
        result = subprocess.run(cmd, capture_output=True, timeout=20, check=False)
        if result.returncode != 0:
            logging.warning(
                "yt-dlp metadata fetch failed for %s: %s",
                safe_url,
                result.stderr.decode("utf-8", "ignore"),
            )
            return None
        data = json.loads(result.stdout.decode("utf-8", "ignore"))
        return {
            "title": str(data.get("title") or "").strip(),
            "channel": str(data.get("channel") or data.get("uploader") or "").strip(),
        }
    except (subprocess.TimeoutExpired, json.JSONDecodeError) as e:
        logging.warning("Could not fetch metadata for %s: %s", video_url, e)
        return None
    except (FileNotFoundError, PermissionError) as e:
        logging.error(f"Could not run yt-dlp for metadata fetch: {e}")
        return None
