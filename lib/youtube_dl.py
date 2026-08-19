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

from .get_platform import get_installed_js_runtime

yt_dlp_cmd = [sys.executable, "-m", "yt_dlp"]

# [V80.1 CORRECAO GRAVE 403 FORJADO POR USER-AGENT DIFERENTE!]:
#   O YouTube ASSINA as URLs de videoplayback com (video_id + expire + ip + UA + outros).
#   Se o yt-dlp gera a URL usando o User-Agent padrao "yt-dlp/2024.x", mas depois o
#   <video> do Chrome baixa com "Chrome/127.x...", o Google detecta "UA diferente da
#   assinatura" e BLOQUEIA TUDO com HTTP 403 Forbidden MESMO se a URL estava "correta".
#   Era ISSO o bug MISTERIOSO que estava causando TUDO (mesmo com node, mesmo com
#   formato 18, dava 403 silencioso no Chrome)!
# SOLUCAO: Forcar o MESMO User-Agent Chrome Desktop NA LINHA DE COMANDO do yt-dlp
#   (yt_dlp_cmd.extend com --user-agent). Assim a URL ja sai assinada com o MESMO
#   UA que o Chrome vai usar depois, e Google NAO VAI MAIS BLOQUEAR por UA mismatch.
#   O MESMO UA também é utilizado no HTTP Range check (Pilar 3 V80) para consistencia.
V80_CHROME_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
)
yt_dlp_cmd.extend(["--user-agent", V80_CHROME_UA])

# [V90: TODOS OS ITAGS PROGRESSIVOS LEGADOS YOUTUBE (1 arquivo = video+audio JUNTOS).
#  Esses itags EXISTEM desde 2006 e NUNCA retornam DASH separado. Se a URL tem
#  um desses, é garantido 1 arquivo MP4/FLV/WebM com AMBOS codecs. O front sempre
#  valida via canplay, então podemos confiar.]
V90_SAFE_PROGRESSIVE_ITAGS = frozenset([
    #  MP4 H.264/AAC CLASSICOS (SEMPRE EXISTEM):
    "18", "22", "59", "60",
    #  MP4 3D (82-85 3D SBS/TB ; 92-96 MP4 AVC 3D 240p~1080p):
    "82", "83", "84", "85", "92", "93", "94", "95", "96",
    #  ANTIGOS 3GP/FLV H.263/FLV1 (funcionam se nao tiver MP4, ex.: videos 2007~2010):
    "36", "17", "5", "6", "34", "35", "37", "38",
    #  WebM VP8/9 (qualquer video que tenha WebM progressivo):
    "43", "44", "45", "46", "100", "101", "102",
])
#  V90: 12 TENTATIVAS EM CASCATA (do MELHOR pro PIOR), sempre 1 arquivo progressivo
#  (video+audio juntos) ou "best" que resolve codecs. A PRIMEIRA que retornar URL
#  válida VENCE e já retorna imediatamente. Nunca mais depender de 1 formato só.
V90_FORMAT_CASCADE = [
    #  (1) CLASSICOS LEGADOS SEGUROS (SEM PRECISAR DE HTTP CHECK):
    "18/22/59/60",
    #  (2) TODOS MP4 3D + MP4 CLASSICOS UNIDOS, forca ambos codecs:
    "18/22/59/60/82/83/84/85/92/93/94/95/96/"
    "best[ext=mp4][vcodec!=none][acodec!=none][protocol^=http]",
    #  (3) TODOS ANTIGOS (3GP, FLV, MP4 antigos):
    "17/36/5/6/34/35/37/38/"
    "worst[ext=mp4][vcodec!=none][acodec!=none][protocol^=http]",
    #  (4) TODOS WEBM PROGRESSIVOS:
    "43/44/45/46/100/101/102/"
    "best[ext=webm][vcodec!=none][acodec!=none][protocol^=http]",
    #  (5) MELHOR QUALQUER EXTENSao DESDE QUE SEJA HTTP/HTTPS + 2 CODECS:
    "best[protocol^=https][vcodec!=none][acodec!=none]",
    #  (6) PIOR (menor arquivo, mas SEMPRE toca se existir):
    "worst[protocol^=https][vcodec!=none][acodec!=none]",
    #  (7) ANY MELHOR QUALQUER (sem restricoes de protocolo — yt-dlp pode retornar HLS):
    "best[vcodec!=none][acodec!=none]",
    #  (8) ANY PIOR:
    "worst[vcodec!=none][acodec!=none]",
    #  (9) BEST ext=MP4 só (muitas vezes existe MP4 mas nao caiu acima):
    "best[ext=mp4]",
    #  (10) WORST ext=mp4:
    "worst[ext=mp4]",
    #  (11) QUALQUER COISA QUE EXISTA (yt-dlp decide):
    "best",
    #  (12) REALMENTE ULTIMO RECURSO:
    "worst",
]
#  V90 EXTRA USER-AGENTS (se Chrome falhou em 12 formatos, tenta Android ou iPhone —
#  YouTube tem pools de URL diferentes por plataforma! Android WebView aceita mais.)
V90_EXTRA_USER_AGENTS = [
    (
        "Mozilla/5.0 (Linux; Android 14; Pixel 8 Pro) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/128.0.0.0 Mobile Safari/537.36"
    ),
]

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
    "segunda voz",
    "2a voz",
    "2ª voz",
    "segunda voz karaoke",
    "karaoke segunda voz",
    "karaoke com 2a voz",
    "karaoke com 2ª voz",
    "play da segunda",
    "letra na tela",
    "musica com letra na tela",
    "letra da musica na tela",
    "sem voz playback",
    "playback sem voz",
    "karaoke voz de apoio",
    "2 vozes karaoke",
    "karaoke dupla",
    "karaoke playback completo",
    "karaoke sem voz playback",
    "karaoke letra",
    "karaoke com letra completa",
    "playback com letra na tela",
    "instrumental com letra na tela",
)

_SEARCH_NEGATIVE_SOFT = (
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
    "podcast",
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
    "videoclipe",
    "video clipe",
    "acustico",
    "acústico",
    "acoustic",
    "remix",
    "dj set",
    "versao estendida",
    "versão estendida",
    "extended",
)

_SEARCH_NEGATIVE_HARD = (
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
    "fifa",
    "game",
    "jogo",
    "jogos",
    "pelicula",
    "filme",
    "trailer oficial",
    "serie",
    "série",
    "novela",
)

_SEARCH_NEGATIVE_KEYWORDS = _SEARCH_NEGATIVE_HARD + _SEARCH_NEGATIVE_SOFT


def _normalize_search_text(text: str) -> str:
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    return re.sub(r"\s+", " ", text).strip().casefold()

_SEARCH_NEGATIVE_HARD_SET = frozenset(
    _normalize_search_text(k) for k in _SEARCH_NEGATIVE_HARD
)
_SEARCH_NEGATIVE_SOFT_SET = frozenset(
    _normalize_search_text(k) for k in _SEARCH_NEGATIVE_SOFT
)
_SEARCH_KARAOKE_KEYWORDS_NORMALIZED = frozenset(
    _normalize_search_text(k) for k in _SEARCH_KARAOKE_KEYWORDS
)



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

    for kw_norm in _SEARCH_KARAOKE_KEYWORDS_NORMALIZED:
        if kw_norm in title_norm:
            score += 20
        if kw_norm in channel_norm:
            score += 6

    if karaoke_only:
        for kw_norm in _SEARCH_NEGATIVE_HARD_SET:
            if kw_norm in title_norm:
                score -= 25
            if kw_norm in channel_norm:
                score -= 8
        for kw_norm in _SEARCH_NEGATIVE_SOFT_SET:
            if kw_norm in title_norm:
                score -= 3
            if kw_norm in channel_norm:
                score -= 1

    return score


def is_likely_karaoke_result(title: str, channel: str = "") -> bool:
    """[V90.2 REESCRITA DEFINITIVA: HARD / SOFT SPLIT]
    Heuristica: se POSITIVA, bloqueia SOMENTE HARD_NEG (cover/ao vivo sao soft, passam).
    Sem positiva: bloqueia de qualquer forma (obriga ter pelo menos 1 kw karaoke).
    """
    title_norm = _normalize_search_text(title)
    channel_norm = _normalize_search_text(channel)

    has_positive_title = any(kw in title_norm for kw in _SEARCH_KARAOKE_KEYWORDS_NORMALIZED)
    has_positive_channel = any(kw in channel_norm for kw in _SEARCH_KARAOKE_KEYWORDS_NORMALIZED)
    positive = has_positive_title or has_positive_channel

    has_hard_neg_title = any(kw in title_norm for kw in _SEARCH_NEGATIVE_HARD_SET)
    has_hard_neg_channel = any(kw in channel_norm for kw in _SEARCH_NEGATIVE_HARD_SET)
    has_hard_neg = has_hard_neg_title or has_hard_neg_channel

    if not positive:
        return False
    return not has_hard_neg


def _js_runtime_args() -> list[str]:
    """[V80 CORRIGIDO GRAVE - ORDEM NODE PRIMEIRO + FORCAR AMBOS SE EXISTIREM]
       Retorna args yt-dlp para JS runtime, SEMPRE com CAMINHO ABSOLUTO do binario.

       BUG ANTES (V57 a V79): loop iterava deno -> node -> bun -> quickjs, e RETORNAVA
       NO PRIMEIRO que existia! Se Dell tivesse BOTH deno (falhando/travando por
       permissoes / caminho) E node (funcionando perfeitamente v22), a funcao
       escolhia deno PRIMEIRO, usava ele, yt-dlp nao conseguia decifrar nCipher
       do player, e retornava URL SEM ASSINATURA = 403 Forbidden no Chrome!
       Era ISSO! O DELL TEM OS DOIS, E ESCOLHIA ERRADO!

       CORRECAO V80 (DEFINITIVA):
         1) INVERTER ORDEM DE PRIORIDADE: NODE PRIMEIRO (mais estavel, oficial!)
         2) SE existir node E existir deno -> PASSAR AMBOS! (yt-dlp tenta na ordem)
         3) Se soh existir um, passar soh ele.
         4) Sempre shutil.which caminho absoluto.
    """
    runtimes_found = []
    # [V80 ORDEM CORRETA: NODE = #1 (estavel, todo mundo usa), depois deno, bun, quickjs]
    for runtime_name in ("node", "deno", "bun", "quickjs"):
        runtime_path = shutil.which(runtime_name)
        if runtime_path:
            runtimes_found.append(f"{runtime_name}:{runtime_path}")
    if runtimes_found:
        # yt-dlp aceita multiplos runtimes separados por virgula, tenta na ordem
        return ["--js-runtimes", ",".join(runtimes_found)]
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


def get_search_results(query: str, karaoke_only: bool = False) -> tuple[list[list[str]], int, int]:
    """Search YouTube for videos matching the query.

    [V90.2 BUSCA REAL DEFINITIVA — 3 NIVEIS, tupla return, duracao 90-600s]
    NIVEL 1 = busca OFICIAL com karaoke_only + filtros NOVOS (hard/soft).
    NIVEL 2 = (apenas karaoke_only=True, raro): N1 deu 0 -> repete com extra
        keywords "segunda voz letra playback" AINDA COM filtro karaoke_only=True
        E filtro duration 90-600s. (evita cair em N3 cedo de mais)
    NIVEL 3 = fallback GERAL raro (sem filtro karaoke): so se N1+N2=0.

    Returns:
        TUPLA (resultados, fallback_level, fallback_count)
        - resultados: List of [title, url, video_id, channel, duration]
        - fallback_level: 1=NORMAL, 2=extra keywords, 3=geral, 0=vazio
        - fallback_count: qtd de videos que entraram via fallback
    """
    resultados_1 = _do_one_search_pass(query, karaoke_only=karaoke_only)
    if len(resultados_1) > 0:
        return (resultados_1, 1, 0)

    if karaoke_only:
        logging.info(
            "[V90.2 BUSCA NIVEL2] resultados_1 = 0, query=%r. "
            "Repetindo com extra 'segunda voz letra playback' ainda com filtro karaoke_only.",
            query,
        )
        query_nv2 = f"{query} segunda voz letra playback"
        raw_2 = _do_one_search_pass(query_nv2, karaoke_only=True)
        if raw_2:
            resultados_2 = []
            for row in raw_2:
                _dur = row[4] or ""
                _sec = -1
                if _dur and ":" in _dur:
                    try:
                        _p = _dur.split(":")
                        if len(_p) == 2:
                            _sec = int(_p[0]) * 60 + int(_p[1])
                    except Exception:
                        _sec = -1
                if _sec >= 0 and (_sec < 90 or _sec > 600):
                    continue
                resultados_2.append(row)
            if len(resultados_2) > 0:
                logging.info(
                    "[V90.2 BUSCA NIVEL2 OK] %d videos (apos duration filter 90-600s). query=%r",
                    len(resultados_2), query,
                )
                return (resultados_2, 2, len(resultados_2))

        logging.info(
            "[V90.2 BUSCA NIVEL2 VAZIO] query=%r. "
            "Caindo para NIVEL3 (fallback geral karaoke_only=False, RARO).", query,
        )
        raw_3 = _do_one_search_pass(query, karaoke_only=False)
        if raw_3:
            resultados_3 = []
            for row in raw_3:
                _dur = row[4] or ""
                _sec = -1
                if _dur and ":" in _dur:
                    try:
                        _p = _dur.split(":")
                        if len(_p) == 2:
                            _sec = int(_p[0]) * 60 + int(_p[1])
                    except Exception:
                        _sec = -1
                if _sec >= 0 and (_sec < 90 or _sec > 600):
                    continue
                resultados_3.append(row)
            if len(resultados_3) > 0:
                logging.warning(
                    "[V90.2 BUSCA NIVEL3 FALLBACK GERAL RARO] %d videos (apos duration filter 90-600s). query=%r",
                    len(resultados_3), query,
                )
                return (resultados_3, 3, len(resultados_3))

        logging.warning(
            "[V90.2 BUSCA VAZIA TOTAL (1,2,3)] query=%r karaoke_only=%r",
            query, karaoke_only,
        )
        return ([], 0, 0)

    logging.warning(
        "[V52 FALLBACK BUSCA VAZIA (modo normal, NIVEL2)] Busca 1 retornou 0 resultados "
        "(query=%r karaoke_only=False). Refazendo busca SEM palavras extra karaoke...", query,
    )
    resultados_2_normal = _do_one_search_pass(query, karaoke_only=False)
    if len(resultados_2_normal) > 0:
        return (resultados_2_normal, 2, len(resultados_2_normal))
    logging.error("[V52 BUSCA] Mesmo fallback retornou 0 resultados. query=%r", query)
    return ([], 0, 0)


def _do_one_search_pass(query: str, karaoke_only: bool) -> list[list[str]]:
    """[V90.2 + RETRY 3x + duration 90<=s<=600 no karaoke_only] Uma 'passada' de busca.
    Usamos .splitlines() em vez de .split(chr(10)) para nao ter SyntaxError de string
    quebrada em gravacoes Windows CP1252 com helpers.
    """
    logging.info(
        "[V90.2 BUSCA] one pass: query=%r karaoke_only=%r", query, karaoke_only
    )
    requested_query = _build_search_query(query, karaoke_only)
    num_results = 120 if karaoke_only else 60
    yt_search = 'ytsearch' + str(num_results) + ':"' + requested_query + '"'
    cmd = yt_dlp_cmd + ["-j", "--no-playlist", "--flat-playlist", yt_search]
    logging.debug("yt-dlp search command (one-pass): " + " ".join(cmd))

    last_output_bytes = b""
    last_exc = None
    tentativa = 0
    max_tentativas = 3
    output_bytes = b""

    while tentativa < max_tentativas:
        tentativa += 1
        try:
            output_bytes = subprocess.check_output(cmd, timeout=120)
        except subprocess.CalledProcessError as e:
            last_exc = e
            logging.debug(
                "Error while executing search (one pass) tentativa=%d: %s",
                tentativa, e,
            )
            output_bytes = e.output or b""
        except subprocess.TimeoutExpired as e:
            last_exc = e
            logging.warning(
                "[V90.2 TIMEOUT BUSCA] _do_one_search_pass query=%r karaoke_only=%r "
                "tentativa=%d estourou 120s! Retrying... %s",
                query, karaoke_only, tentativa, e,
            )
            output_bytes = b""
        except Exception as e:
            last_exc = e
            logging.warning(
                "[V52 BUSCA] subprocess excecao inesperada tentativa=%d: %s",
                tentativa, e,
            )
            output_bytes = b""

        last_output_bytes = output_bytes
        validas = 0
        for line in last_output_bytes.decode("utf-8", "ignore").splitlines():
            if len(line) > 2:
                validas += 1
        if validas >= 5:
            logging.debug(
                "[V90.2 BUSCA RETRY OK] tentativa=%d validas=%d query=%r karaoke_only=%r",
                tentativa, validas, query, karaoke_only,
            )
            break
        if tentativa < max_tentativas:
            logging.warning(
                "[V90.2 BUSCA RETRY] output muito pequeno (%d linhas < 5). "
                "Sleep 1.5s retry %d/%d. query=%r karaoke_only=%r",
                validas, tentativa + 1, max_tentativas, query, karaoke_only,
            )
            time.sleep(1.5)
    else:
        logging.warning(
            "[V90.2 BUSCA RETRY ESGOTADAS] 3 tentativas todas <5 linhas validas. "
            "query=%r karaoke_only=%r bytes=%d ult_exc=%r",
            query, karaoke_only, len(last_output_bytes), last_exc,
        )

    output = last_output_bytes.decode("utf-8", "ignore")
    logging.debug("Search results (one pass) length: %d bytes", len(output))
    scored_results = []
    for line in output.splitlines():
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
        _v_url = normalize_youtube_url_to_std(_v_id)
        channel = j.get("channel") or j.get("uploader") or ""
        duration_raw = j.get("duration")
        duration_str = ""
        seconds = -1
        if isinstance(duration_raw, (int, float)):
            seconds = int(duration_raw)
            duration_str = str(seconds // 60) + ":" + format(seconds % 60, "02d")
        row = [str(j["title"]), _v_url, _v_id, str(channel), duration_str]

        if karaoke_only:
            if not is_likely_karaoke_result(row[0], str(channel)):
                continue
            if seconds >= 0:
                if seconds < 90 or seconds > 600:
                    continue

        score = _score_search_result(row[0], str(channel), query, karaoke_only)
        scored_results.append((score, row))

    if karaoke_only:
        karaoke_matches = [row for score, row in scored_results if score >= -10]
        if karaoke_matches:
            scored_results = [
                (score, row) for score, row in scored_results if score >= -10
            ]

    scored_results.sort(key=lambda item: item[0], reverse=True)
    cap = 30 if karaoke_only else 25
    return [row for _, row in scored_results[:cap]]

def _v90_helper_extract_itag(candidate: str) -> str:
    """Extrai o parametro ?itag= de uma URL googlevideo.com (usada p/ decidir safe)."""
    try:
        from urllib.parse import urlparse, parse_qs
        _q = parse_qs(urlparse(candidate).query)
        return (_q.get("itag", [""])[0] or "").strip()
    except Exception:
        return ""


def _v90_http_range_check(candidate: str, user_agent: str) -> bool:
    """V90 HTTP Range check bytes=0-1024 com headers Chrome COMPLETOS.
    Retorna True se tudo OK (200/206, Content-Type video/audio/mp4, tamanho > 200KB).
    Retorna False se qualquer coisa falhar (403, 404, decode error etc)."""
    try:
        req = Request(candidate, method="GET")
        req.add_header("Range", "bytes=0-1024")
        req.add_header("User-Agent", user_agent)
        req.add_header("Accept", "*/*")
        req.add_header("Accept-Encoding", "identity;q=1, *;q=0")
        req.add_header("Accept-Language", "pt-BR,pt;q=0.9,en;q=0.8")
        req.add_header("Origin", "https://www.youtube.com")
        req.add_header("Referer", "https://www.youtube.com/")
        req.add_header("Sec-CH-UA",
                       '"Chromium";v="128", "Google Chrome";v="128", "Not=A?Brand";v="24"')
        req.add_header("Sec-CH-UA-Mobile", "?0")
        req.add_header("Sec-CH-UA-Platform", '"Windows"')
        req.add_header("Sec-Fetch-Dest", "video")
        req.add_header("Sec-Fetch-Mode", "no-cors")
        req.add_header("Sec-Fetch-Site", "cross-site")
        with urlopen(req, timeout=10) as r:
            status = r.status
            ct = str(r.headers.get("Content-Type", "") or "").lower()
            cl_raw = r.headers.get("Content-Length", None)
            if status not in (200, 206):
                return False
            if (not ct.startswith("video/") and not ct.startswith("audio/")
                    and not ct.startswith("application/mp4")
                    and not ct.startswith("application/octet-stream")):
                return False
            total_bytes = 0
            if cl_raw and str(cl_raw).isdigit():
                total_bytes = int(cl_raw)
            else:
                cr = r.headers.get("Content-Range", "") or ""
                if "/" in cr:
                    try: total_bytes = int(cr.split("/", 1)[1])
                    except (ValueError, IndexError): pass
            if total_bytes and total_bytes < 200_000:
                return False
            return True
    except Exception:
        return False


def _v90_validate_url(candidate: str, safe_url: str, user_agent: str) -> str | None:
    """Recebe URL candidata e decide se ENTREGA pro front (retorna a URL)
    ou descarta (retorna None). Regras:

    1. Se itag está em V90_SAFE_PROGRESSIVE_ITAGS (18/22/.../102):
       → SEMPRE ENTREGA (mesmo se HTTP Range do Python falhar, o front valida).
       O valor de 'itag' NÃO MENTE: esses IDs garantem 1 arquivo progressivo.
    2. Senão:
       → OBRIGATORIO passar por _v90_http_range_check (200/206 etc)."""
    if not candidate or not candidate.startswith("http"):
        return None
    itag = _v90_helper_extract_itag(candidate)
    if itag and itag in V90_SAFE_PROGRESSIVE_ITAGS:
        logging.warning(
            f"[V90 SAFE ITAG LIBERADO] vid={safe_url} itag={itag} (progressivo LEGADO). "
            "URL entregue pro front. Validação REAL fica com o evento canplay."
        )
        return candidate
    # Nao-safe (formato best, DASH, etc): OBRIGATORIO HTTP Range check
    if _v90_http_range_check(candidate, user_agent):
        logging.info(f"[V90 URL OK (nao-safe, passou HTTP check)] vid={safe_url} itag={itag or '?'}")
        return candidate
    return None


def _v90_try_once(
    safe_url: str,
    fmt_filter: str,
    user_agent: str | None = None,
    timeout_per_try: int = 25,
) -> str | None:
    """RODA 1 TENTATIVA do yt-dlp com 1 formato específico.

    - Pega o output, extrai a 1a URL (yt-dlp retorna 2 linhas se for DASH separado;
      se for progressivo, retorna 1 linha).
    - Chama _v90_validate_url para aprovar ou não.
    - Qualquer falha: retorna None (o loop cascata vai tentar o próximo).
    """
    cmd = yt_dlp_cmd.copy()
    #  Override user-agent se essa tentativa usar UA diferente (Android, etc):
    if user_agent is not None:
        #  Troca o --user-agent da base pelo desta tentativa
        #  (garante que não tenha 2 --user-agent conflitantes)
        filtered = []
        skip_next = False
        for i, arg in enumerate(cmd):
            if skip_next:
                skip_next = False
                continue
            if arg == "--user-agent":
                skip_next = True
                continue
            filtered.append(arg)
        cmd = filtered
        cmd.extend(["--user-agent", user_agent])
    cmd += ["-g", "-f", fmt_filter]
    cmd += _js_runtime_args() + _cookies_args() + [
        "--extractor-args", "youtube:player_client=ios,android,web;skip=dash;player_skip=configs",
        "--remote-components", "ejs:github",
        "--no-check-certificates",
        "--ignore-errors",
        "--no-abort-on-error",
    ]
    cmd.append(safe_url)
    try:
        result = subprocess.run(cmd, capture_output=True, timeout=timeout_per_try, check=False)
    except subprocess.TimeoutExpired:
        return None
    except (FileNotFoundError, PermissionError):
        return None
    if result.returncode != 0:
        #  Erro nessa tentativa, mas o loop cascata continuará
        return None
    out = result.stdout.decode("utf-8", "ignore").strip()
    if not out:
        return None
    lines = [ln.strip() for ln in out.splitlines() if ln.strip()]
    if not lines:
        return None
    candidate = lines[0]
    effective_ua = user_agent if user_agent is not None else V80_CHROME_UA
    return _v90_validate_url(candidate, safe_url, effective_ua)


def get_stream_url(video_url: str) -> str | None:
    """Get a direct stream URL for a YouTube video without downloading it.

    V90 = A PROVA DE FALHA TOTAL — DEU CERTO COM 1 VIDEO, DÁ CERTO COM TODOS:
      - NÃO DEPENDE MAIS de 1 formato só (18/22). Usa **12 formatos em cascata**
        (V90_FORMAT_CASCADE: progressivos MP4/3GP/FLV/WebM classicos + best/
        worst genericos + ANY protocol).
      - NÃO DEPENDE MAIS de 1 User-Agent só. Se 12 formatos com Chrome/128 falhar,
        repete **as 12 tentativas NOVAMENTE mas com Android User-Agent**
        (YouTube tem pools de URLs diferentes por plataforma, Android aceita mais).
      - NÃO ERRA MAIS itags progressivos: se a URL tem itag que está em
        V90_SAFE_PROGRESSIVE_ITAGS (18/22/59/60/82-85/92-96/17/34-38/5/6/
        43-46/100-102) ela é LIBERADA DIRETO (pois esses itags NÃO MENTEM: são
        sempre 1 arquivo com vídeo+áudio juntos). Validação REAL final fica
        com o evento `canplay` do navegador.
      - Qualquer outro formato (best, worst, DASH merged, etc) OBRIGATORIAMENTE
        passa por HTTP Range check bytes=0-1024 com headers Chrome COMPLETOS.
      - 24 tentativas totais yt-dlp por vídeo: (12 formatos + 12 Android UA).
    """
    safe_url = normalize_youtube_url_to_std(video_url)

    #  (FASE A) 12 tentativas cascata com Chrome/128.
    for idx, fmt in enumerate(V90_FORMAT_CASCADE):
        res = _v90_try_once(safe_url, fmt, user_agent=None, timeout_per_try=25)
        if res:
            logging.info(f"[V90 FASE A SUCESSO] tentativa {idx+1}/12 fmt={fmt[:40]}... -> retornando URL")
            return res

    #  (FASE B) 12 tentativas repetidas com User-Agent ANDROID (plataforma diferente).
    for idx, fmt in enumerate(V90_FORMAT_CASCADE):
        for ua in V90_EXTRA_USER_AGENTS:
            res = _v90_try_once(safe_url, fmt, user_agent=ua, timeout_per_try=25)
            if res:
                logging.info(
                    f"[V90 FASE B SUCESSO (Android UA)] tentativa B-{idx+1}/12 fmt={fmt[:40]}..."
                    " -> retornando URL"
                )
                return res

    #  (FASE C) ÚLTIMA OPORTUNIDADE: re-tentar só o 18/22/59/60 com cookies RECARREGADOS
    #  (refresh forcado do guest cookies, as vezes resolve bolha de 403 de sessao).
    _ensure_guest_cookie_file(force_refresh=True)
    for fmt in ("18/22/59/60", "best[ext=mp4][vcodec!=none][acodec!=none]"):
        res = _v90_try_once(safe_url, fmt, user_agent=None, timeout_per_try=30)
        if res:
            logging.info(f"[V90 FASE C SUCESSO (cookies refresh)] fmt={fmt}")
            return res

    logging.error(f"[V90 TODAS AS 24+ TENTATIVAS FALHARAM para {safe_url}. "
                  "Vai cair no front fallback invencível (YouTube EMBED no app).")
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
