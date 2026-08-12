"""
Singer Manager (GERENCIADOR DE LISTA PARALELA DE CANTORES - FASE 1 + FASE 2 AMARRACAO)
Armazena lista de cantores com:
  - Ordem de chegada (SAGRADA - nunca quebra, novos sempre NO FIM)
  - Status por cantor:
      waiting       = chegou, tem musica pendente na fila
      pending_music = chegou, NAO TEM MUSICA AINDA (chamar operador!)
      skipping      = nao quer cantar ESTA rodada (comendo/bebendo), volta p/ fim
      left_early    = saiu mais cedo (nao vai mais cantar hoje)
      sung          = acabou de cantar (proxima rodada dele eh no fim)
  - Aliases (apelidos/nomes na fila real) VINCULADOS MANUALMENTE pelo operador.
  - Matching engine: dado user da fila real -> encontra o cantor (nome ou apelido).
  - Persistencia em JSON (dados sobrevivem restart smartokepy, aliases tambem).
  - Thread-safe (lock).

FASE 2: Amarrado com queue_manager. O karaoke.py ouve eventos playback_started /
queue_update / now_playing_update e chama:
  - SingerManager.mark_singer_sung_by_queue_user(user) -> marca cantor como sung
  - SingerManager.refresh_statuses_from_queue(itens_fila, now_user, next_user) -> atualiza
    automaticamente status pending_music / waiting + quem esta tocando agora / proximo.
"""
from __future__ import annotations

import json
import re
import threading
import time
import unicodedata
import uuid as _uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional


STATUS_WAITING = "waiting"
STATUS_PENDING_MUSIC = "pending_music"
STATUS_SKIPPING = "skipping"
STATUS_LEFT_EARLY = "left_early"
STATUS_SUNG = "sung"

STATUS_LABEL = {
    STATUS_WAITING: "Aguardando vez",
    STATUS_PENDING_MUSIC: "SEM MÚSICA - Chamar!",
    STATUS_SKIPPING: "Pulou esta rodada (comendo/bebendo)",
    STATUS_LEFT_EARLY: "Saiu mais cedo",
    STATUS_SUNG: "Já cantou nesta rodada",
}

STATUS_COLOR = {
    STATUS_WAITING: "#34d399",
    STATUS_PENDING_MUSIC: "#ef4444",
    STATUS_SKIPPING: "#f59e0b",
    STATUS_LEFT_EARLY: "#64748b",
    STATUS_SUNG: "#60a5fa",
}


# Nomes de sistema ignorados (Randomizer, Pikaraoke, etc). Nunca linkam com cantor real.
IGNORED_SYSTEM_USERS = {
    "randomizer",
    "pikaraoke",
    "sistema",
    "system",
    "aleatorio",
    "karaoke",
    "smartokepy",
    "queue",
}


def _normalize_name(text: str) -> str:
    """Normaliza nome para matching: sem acentos, lowercase, espacos extras, sem pontuacao de mesa.

    Remove prefixos "mesa N:", "mesa N -", "m N:" etc. Ex:
      "mesa-5: Carlos" -> "carlos"
      "José das Coves" -> "jose das coves"
      "Maria  " -> "maria"
    """
    if not text:
        return ""
    t = unicodedata.normalize("NFKD", str(text)).encode("ascii", "ignore").decode("ascii")
    t = t.lower().strip()
    # tira prefixo mesa/mesaN/m-N/mesa-N numero:
    t = re.sub(r"^(mesa|m)[\s\-\:]*\d+[\s\-\:]+", "", t)
    t = re.sub(r"\s+", " ", t).strip(" -:,.")
    return t


@dataclass
class Singer:
    singer_id: str
    name: str
    arrival_unix: float
    arrival_visual_unix: float = 0.0  # REGRA SAGRADA: NUNCA MUDA depois de criar. Ordem VISUAL (1,2,3) = sort por este campo. arrival_unix pode mudar (logica rodada proximo cantor) MAS NAO A ORDEM DOS CARDS.
    status: str = STATUS_PENDING_MUSIC
    note: str = ""
    rounds_sung: int = 0
    songs_added_total: int = 0
    aliases: list = field(default_factory=list)
    created_at_unix: float = field(default_factory=time.time)
    updated_at_unix: float = field(default_factory=time.time)


class SingerManager:
    """Gerenciador thread-safe de lista de cantores (ordem de chegada sagrada)."""

    def __init__(self, persist_path: Optional[str] = None) -> None:
        self._lock = threading.RLock()
        self._singers: list[Singer] = []
        if persist_path is None:
            try:
                from pikaraoke.lib.get_platform import get_data_directory

                base = Path(get_data_directory())
            except Exception:
                base = Path.home() / ".pikaraoke"
            base.mkdir(parents=True, exist_ok=True)
            persist_path = str(base / "singers_state_fase1.json")
        self._persist_path = Path(persist_path)
        self._load()

    @staticmethod
    def _normalize_name(text: str) -> str:
        # wrapper estatico para _normalize_name (evitar circular; ja existe global)
        return _normalize_name(text)

    @staticmethod
    def _is_status_already_in_rodada(singer: Singer) -> bool:
        """Retorna True se o cantor JA FOI processado nesta rodada (nao esta na fila de 'ainda vao cantar').
        Ou seja: status SUNG / SKIPPING / LEFT_EARLY.
        Os demais (WAITING / PENDING_MUSIC) ainda NAO cantaram e vem PRIMEIRO na exibicao/proximo_calculo.
        """
        return singer.status in (STATUS_SUNG, STATUS_SKIPPING, STATUS_LEFT_EARLY)

    def _reordered_by_rodada(self, singers_list: Optional[list[Singer]] = None) -> list[Singer]:
        """Reordena a lista de cantores CONFORME REGRA DE RODADA (NAO QUEBRA ORDEM DE CHEGADA SAGRADA):

        GRUPO 1 = PRIMEIRO: Ainda NAO cantaram nesta rodada (status WAITING / PENDING_MUSIC).
                 → Ordenado por arrival_unix CRESCENTE = ORDEM DE CHEGADA SAGRADA.

        GRUPO 2 = DEPOIS: Ja cantaram / pulou / saiu cedo (status SUNG / SKIPPING / LEFT_EARLY).
                 → Ordenado por arrival_unix CRESCENTE (timestamp atualizado quando virou SUNG/SKIPPING,
                   que representa "proxima rodada").

        Usado NO ATO DO INGRESSO (add_singer), no update_status, no to_dict, no refresh_statuses_from_queue.
        Sempre preserva ordem de chegada SAGRADA (chegou primeiro -> primeiro de seu grupo).
        """
        src = list(singers_list) if singers_list is not None else list(self._singers)
        grupo_ainda_nao_cantaram: list[Singer] = []
        grupo_ja_foram_da_rodada: list[Singer] = []
        for s in src:
            if self._is_status_already_in_rodada(s):
                grupo_ja_foram_da_rodada.append(s)
            else:
                grupo_ainda_nao_cantaram.append(s)
        grupo_ainda_nao_cantaram.sort(key=lambda s: s.arrival_unix)
        grupo_ja_foram_da_rodada.sort(key=lambda s: s.arrival_unix)
        return grupo_ainda_nao_cantaram + grupo_ja_foram_da_rodada

    # ------------------------------ serializacao ------------------------------
    def to_dict(self, include_left_early: bool = True) -> dict:
        with self._lock:
            # REGRA SAGRADA DO USUARIO (2026-08-12): ORDEM VISUAL = ORDEM DE CHEGADA, NUNCA MUDA!
            # - arrival_visual_unix = DATA E HORA QUE ELE ENTROU (IMUTAVEL, NUNCA MAIS ALTERADO)
            # - Os cards SEMPRE aparecem em ORDEM CRESCENTE de arrival_visual_unix.
            # - arrival_unix PODE ser atualizado (logica de rodada para calcular PROXIMO cantor),
            #   mas ISSO NUNCA INFLUENCIA NA POSICAO VISUAL (numero 1/2/3).
            # - Badges (CANTANDO AGORA / PROXIMO / Já cantou 1x) MUDAM, posicao NAO.
            singers = [s for s in self._singers if include_left_early or s.status != STATUS_LEFT_EARLY]
            singers.sort(key=lambda s: s.arrival_visual_unix)
            return {
                "generated_at": time.time(),
                "total": len(singers),
                "singers": [
                    {
                        **asdict(s),
                        "status_label": STATUS_LABEL.get(s.status, s.status),
                        "status_color": STATUS_COLOR.get(s.status, "#94a3b8"),
                        "arrival_str": time.strftime(
                            "%H:%M", time.localtime(s.arrival_visual_unix)  # mostra a HORA QUE ELE CHEGOU MESMO, nunca muda!
                        ),
                        "aliases_display": [str(a) for a in (s.aliases or [])],
                    }
                    for s in singers
                ],
            }

    # ------------------------------ helpers aliases / matching ------------------------------
    def _singer_has_name(self, singer: Singer, normalized: str) -> bool:
        if not normalized:
            return False
        if _normalize_name(singer.name) == normalized:
            return True
        for alias in (singer.aliases or []):
            if _normalize_name(str(alias)) == normalized:
                return True
        return False

    def find_singer_by_queue_user(self, queue_user: str) -> Optional[Singer]:
        """FASE 2 Matching Engine: dado 'user' de item da fila real -> retorna Singer ou None.

        - Ignora nomes de sistema (Randomizer, Pikaraoke etc).
        - Match por nome principal do cantor OU qualquer alias dele (normalizado).
        """
        queue_user = (queue_user or "").strip()
        if not queue_user:
            return None
        norm = _normalize_name(queue_user)
        if not norm:
            return None
        if norm in IGNORED_SYSTEM_USERS:
            return None
        with self._lock:
            # primeiro por match exato (normalizado)
            for s in self._singers:
                if self._singer_has_name(s, norm):
                    return s
            return None

    def add_alias(self, singer_id: str, alias: str) -> Optional[Singer]:
        alias = (alias or "").strip()
        if not alias or not singer_id:
            return None
        with self._lock:
            for s in self._singers:
                if s.singer_id == singer_id:
                    aliases = [str(a) for a in (s.aliases or [])]
                    alias_norm = _normalize_name(alias)
                    alias_display = alias[:120]
                    # Nao duplica (normalizado)
                    for existing in aliases:
                        if _normalize_name(existing) == alias_norm:
                            return s
                    aliases.append(alias_display)
                    s.aliases = aliases
                    s.updated_at_unix = time.time()
                    self._save()
                    return s
            return None

    def remove_alias(self, singer_id: str, alias: str) -> Optional[Singer]:
        alias = (alias or "").strip()
        if not alias or not singer_id:
            return None
        norm = _normalize_name(alias)
        with self._lock:
            for s in self._singers:
                if s.singer_id == singer_id:
                    aliases = [a for a in (s.aliases or []) if _normalize_name(str(a)) != norm]
                    s.aliases = aliases
                    s.updated_at_unix = time.time()
                    self._save()
                    return s
            return None

    def set_aliases_from_csv(self, singer_id: str, csv_text: str) -> Optional[Singer]:
        """Salva apelidos a partir de um texto CSV (separados por virgula/ponto e virgula)."""
        if not singer_id:
            return None
        parts = [p.strip() for p in re.split(r"[,;]", str(csv_text or "")) if p and p.strip()]
        seen = set()
        unicos = []
        for p in parts:
            n = _normalize_name(p)
            if not n or n in seen:
                continue
            seen.add(n)
            unicos.append(p[:120])
        with self._lock:
            for s in self._singers:
                if s.singer_id == singer_id:
                    s.aliases = unicos
                    s.updated_at_unix = time.time()
                    self._save()
                    return s
            return None

    def get_unmatched_queue_users(self, queue_items: list) -> list[dict]:
        """Retorna nomes de 'user' da fila real que AINDA NAO foram linkados a nenhum cantor.

        Usado no topo da UI para sugerir "clique aqui para ligar a José Mesa 1 ao José das Coves".
        """
        seen_norm = set()
        result = []
        for it in (queue_items or []):
            user = str(it.get("user") or "").strip()
            if not user:
                continue
            n = _normalize_name(user)
            if not n or n in IGNORED_SYSTEM_USERS or n in seen_norm:
                continue
            seen_norm.add(n)
            # tem cantor linkado?
            linked = None
            with self._lock:
                for s in self._singers:
                    if self._singer_has_name(s, n):
                        linked = s.singer_id
                        break
            if linked is None:
                result.append({"user": user, "user_norm": n, "title": str(it.get("title") or "")[:120]})
        return result

    # ------------------------------ API FASE 2 AUTOMACAO ------------------------------
    def mark_singer_sung_by_queue_user(self, queue_user: str) -> Optional[Singer]:
        """FASE 2 AUTOMACAO 1: Chamado quando musica COMECOU (playback_started).
        Pega o user da musica que esta tocando agora -> marca cantor como SUNG
        (acrescenta rounds_sung + 1 e move para o FIM da rodada)."""
        s = self.find_singer_by_queue_user(queue_user)
        if s is None:
            return None
        return self.update_status(s.singer_id, STATUS_SUNG)

    # ======================================================================================
    # [LEI ABSOLUTA - VEZ SAGRADA 1→2→3→4→1→2→3→4]
    # Nenhum cantor NUNCA, NUNCA toca antes de quem chegou primeiro, INDEPENDENTEMENTE
    # da ordem que adicionaram música na fila real.
    # Ex: Junior #1 chegou 09:48, Simone #2 16:26, Maria #3 16:49, Joao #4 16:49.
    # Se Joao #4 adicionar 10 musicas AGORA e Junior #1 ainda nao tem nenhuma →
    # JOAO #4 NAO TOCA. SISTEMA BLOQUEIA e CHAMA JUNIOR #1. So ele toca primeiro.
    # ======================================================================================

    def matches_queue_user(self, singer: Optional[Singer], queue_user: str) -> bool:
        """Retorna True SE E SOMENTE SE o user da fila REAL (queue item.user) combina
        com o Singer paralelo: nome batendo OU algum alias (apelido) cadastrado batendo.
        Normaliza ambos os lados: sem acentos, lowercase, espacos extras, mesa prefixado.
        Nao combina nunca com system users (randomizer/pikaraoke etc)."""
        if singer is None or not queue_user:
            return False
        qn = _normalize_name(str(queue_user))
        if not qn or qn in IGNORED_SYSTEM_USERS:
            return False
        if _normalize_name(singer.name) == qn:
            return True
        for alia in (singer.aliases or []):
            if _normalize_name(str(alia)) == qn:
                return True
        return False

    def get_next_singer_that_should_sing_now(
        self,
        now_playing_user: str | None,
        queue_items: list | None,
    ) -> tuple[Optional[Singer], str]:
        """LEI ABSOLUTA: retorna QUEM é o PRÓXIMO cantor na ORDEM VISUAL DE CHEGADA
        que DEVE cantar AGORA. NÃO IMPORTA quem já tem música na fila real em primeiro.
        SÓ IMPORTA a ordem de chegada 1→2→3→4 + regras de rodada (pular skipping/left_early,
        GRUPO1 ainda não cantou, GRUPO2 nova rodada CIRCULAR depois do now).

        Usado para: (1) obrigar o queue_manager a tocar SOMENTE música desse cantor,
        (2) se o cantor não tem nenhuma música na fila real, NINGUÉM toca (bloqueio
        total, chamada URGENTE para ele escolher música).

        Returns:
            (Singer | None, nome_display_para_log)."""
        with self._lock:
            all_singers_list = list(self._singers) if isinstance(self._singers, list) else list(getattr(self._singers, "values", lambda: [])())
            all_singers_visual_order = sorted(
                all_singers_list,
                key=lambda s: s.arrival_visual_unix
            )
            _by_id: dict[str, Singer] = {}
            for s in all_singers_visual_order:
                try: _by_id[str(s.singer_id)] = s
                except Exception: pass
            if not all_singers_visual_order:
                return (None, "")

            now_singer_id: str | None = None
            if now_playing_user:
                s_now = self.find_singer_by_queue_user(str(now_playing_user))
                if s_now is not None:
                    now_singer_id = s_now.singer_id

            # === MESMA LOGICA do refresh_statuses_from_queue (imune a bugs de SUNG fantasma) ===
            def _ainda_nao_cantou_rodada(s: Singer) -> bool:
                if now_singer_id and s.singer_id == now_singer_id:
                    return True
                return s.status not in (STATUS_SUNG, STATUS_SKIPPING, STATUS_LEFT_EARLY)

            # (1) ORDEM VISUAL SAGRADA: arrival_visual_unix CRESCENTE (1,2,3,4 - NUNCA MUDA).
            # (2) GRUPO 1 PRIORIDADE MAX: Ainda nao cantou nesta rodada.
            #     → PRIMEIRO deste grupo, ORDEM VISUAL, PULANDO o now + pulando SKIPPING.
            next_singer_id_rodada: str | None = None
            for s in all_singers_visual_order:
                if now_singer_id and s.singer_id == now_singer_id:
                    continue
                if s.status in (STATUS_LEFT_EARLY,):
                    continue
                if s.status in (STATUS_SKIPPING,):
                    continue
                if _ainda_nao_cantou_rodada(s):
                    next_singer_id_rodada = s.singer_id
                    break
            # (3) GRUPO 2 (Soh se GRUPO1 VAZIO = TODOS JA CANTARAM DE VERDADE: NOVA RODADA CIRCULAR).
            if not next_singer_id_rodada:
                now_visual_idx: int | None = None
                for i, s in enumerate(all_singers_visual_order):
                    if now_singer_id and s.singer_id == now_singer_id:
                        now_visual_idx = i
                        break
                # (3a) Primeiro SUNG DEPOIS do now na ordem visual (ignora SKIPPING e LEFT_EARLY)
                found: str | None = None
                for i, s in enumerate(all_singers_visual_order):
                    if now_singer_id and s.singer_id == now_singer_id:
                        continue
                    if s.status in (STATUS_LEFT_EARLY, STATUS_SKIPPING):
                        continue
                    if now_visual_idx is not None and i <= now_visual_idx:
                        continue
                    if s.status in (STATUS_SUNG,):
                        found = s.singer_id
                        break
                if not found:
                    # (3b) ninguem depois → volta pro começo da lista (nova rodada)
                    for i, s in enumerate(all_singers_visual_order):
                        if now_singer_id and s.singer_id == now_singer_id:
                            continue
                        if s.status in (STATUS_LEFT_EARLY, STATUS_SKIPPING):
                            continue
                        if s.status in (STATUS_SUNG,):
                            found = s.singer_id
                            break
                next_singer_id_rodada = found

            # Fallback: se GRUPO1 nao tinha ninguem por causa de SKIPPING, agora
            # TENTA INCLUIR os SKIPPING com menor prioridade (nao tem mais ninguem hoje).
            # Nao inclui LEFT_EARLY nunca.
            if not next_singer_id_rodada:
                for s in all_singers_visual_order:
                    if now_singer_id and s.singer_id == now_singer_id:
                        continue
                    if s.status in (STATUS_LEFT_EARLY,):
                        continue
                    next_singer_id_rodada = s.singer_id
                    break
            if not next_singer_id_rodada:
                return (None, "")

            next_singer = _by_id.get(str(next_singer_id_rodada))
            if next_singer is None:
                return (None, "")
            return (next_singer, str(next_singer.name))

    def refresh_statuses_from_queue(self, queue_items: list,
                                    now_playing_user: Optional[str] = None,
                                    next_user: Optional[str] = None) -> dict:
        """FASE 2 AUTOMACAO 2: Sync status dos cantores a partir da FILA REAL.

        Regras (NAO SOBRESCREVE status pulando/saiu cedo marcados manualmente!):
          - Cantor TEM MUSICA NA FILA (agora, proximo, ou qualquer posicao) -> status = WAITING.
          - Cantor NAO TEM NENHUMA musica na fila E status atual e PENDING_MUSIC -> fica.
          - Cantor NAO TEM NENHUMA musica na fila E estava WAITING/SUNG -> volta PENDING_MUSIC
            (ele terminou todas musicas que tinha; chamar novamente para saber se vai querer outra).
          - Status SKIPPING / LEFT_EARLY -> NUNCA toca (operador que definiu manualmente).

        Retorna: dict {changed: int, now_singer_id: str|None, next_singer_id: str|None,
                       unmatched: list[dict], now_queue_user: str, next_queue_user: str}
        """
        changed = 0
        # monta set de users presentes na fila real (normalizados)
        queue_users_norm: set[str] = set()
        for it in (queue_items or []):
            user = str(it.get("user") or "").strip()
            if user:
                n = _normalize_name(user)
                if n and n not in IGNORED_SYSTEM_USERS:
                    queue_users_norm.add(n)

        now_norm = _normalize_name(now_playing_user or "")
        next_norm = _normalize_name(next_user or "")
        if now_norm and now_norm in IGNORED_SYSTEM_USERS:
            now_norm = ""
        if next_norm and next_norm in IGNORED_SYSTEM_USERS:
            next_norm = ""

        now_singer_id = None
        next_singer_id = None

        with self._lock:
            for s in self._singers:
                has_music = False
                s_norm = _normalize_name(s.name)
                if s_norm and s_norm in queue_users_norm:
                    has_music = True
                if not has_music:
                    for a in (s.aliases or []):
                        an = _normalize_name(str(a))
                        if an and an in queue_users_norm:
                            has_music = True
                            break
                # se nao tem nada na fila mas EH O DONO DA MUSICA ATUAL OU DO PROXIMO (borda) -> tem musica
                if not has_music and (
                    (now_norm and self._singer_has_name(s, now_norm)) or
                    (next_norm and self._singer_has_name(s, next_norm))
                ):
                    has_music = True
                # marca who is now / next singer id
                if now_norm and self._singer_has_name(s, now_norm):
                    now_singer_id = s.singer_id
                if next_norm and self._singer_has_name(s, next_norm):
                    next_singer_id = s.singer_id

                # Aplica regras de status (SO SE NAO EH skipping/left_early)
                if s.status in (STATUS_SKIPPING, STATUS_LEFT_EARLY):
                    continue
                if has_music:
                    # REGRA DE RODADA: se JA CANTOU (SUNG), NAO volta para WAITING nesta rodada.
                    # Ele espera a PROXIMA rodada para cantar novamente (todo mundo que ainda nao
                    # cantou vai primeiro). Operador marca SUNG = "ele ja teve sua vez aqui".
                    if s.status != STATUS_SUNG and s.status != STATUS_WAITING:
                        # transiciona PENDING_MUSIC / SKIPPING -> WAITING (so altera se necessario)
                        old = s.status
                        s.status = STATUS_WAITING
                        s.updated_at_unix = time.time()
                        if old != s.status:
                            changed += 1
                else:
                    # sem musica; se estava WAITING -> volta PENDING_MUSIC (chamar de novo).
                    # REGRA DE RODADA: se estava SUNG (ja cantou nesta rodada), CONTINUA SUNG.
                    # NAO volta PENDING_MUSIC na mesma rodada so pq ficou sem musica temporariamente.
                    if s.status == STATUS_WAITING:
                        s.status = STATUS_PENDING_MUSIC
                        s.updated_at_unix = time.time()
                        changed += 1
            if changed:
                self._save()

        # =============== REGRA SAGRADA 2026-08-12: next_singer_id = SEMPRE ORDEM VISUAL (POSICAO 1,2,3,4), NUNCA PULA NINGUEM ===============
        # BUG PASSADO 2026-08-12 (USUARIO RECLAMACAO): Junior (#1) cantando agora, Proximo era Joao (#4)
        # pulando Simone (#2) e Maria (#3). Causa: status de Simone e Maria estava "SUNG" (marcado
        # errado por bug anterior no playback_started). Na logica antiga, eles iam para GRUPO2 e o
        # unico GRUPO1 era Joao → pulava todo mundo!
        #
        # LOGICA CORRETA IMEDIATA (nunca pula, imune a status fantasmas SUNG do passado):
        #   (0) CANTANDO AGORA (now_singer_id) = SEMPRE CONSIDERADO "AINDA NAO CANTOU ESTA RODADA"
        #       (mesmo que por bug status seja SUNG / SKIPPING / etc. — ele esta no MEIO da musica,
        #        a vez dele NAO ACABOU! Ele nunca eh o proximo (pulamos ele no for), mas nao deixa
        #        buraco na contagem de "quem ja foi").
        #   (1) ORDEM VISUAL SAGRADA = arrival_visual_unix CRESCENTE (posicao 1,2,3,4 — NUNCA MUDA!).
        #   (2) GRUPO 1 (PRIORIDADE MAIOR): quem AINDA NAO CANTOU nesta rodada.
        #       = (eh o CANTANDO AGORA) OU (status NOT IN (SUNG, SKIPPING, LEFT_EARLY)).
        #       → PRIMEIRO deste grupo, na ORDEM VISUAL, PULANDO o CANTANDO AGORA = PROXIMO(A).
        #   (3) GRUPO 2 (Soh se GRUPO1 VAZIO = TODOS JA CANTARAM DE VERDADE): NOVA RODADA COMECA.
        #       → PRIMEIRO da ORDEM VISUAL, status=SUNG, PULANDO o CANTANDO AGORA (se houver).
        #   (4) Fallback: SKIPPING e depois next_user da fila.
        #
        # Com isso:
        #   Se Junior (#1) = CANTANDO → GRUPO1 tem Simone/Maria/Joao → Proximo = Simone (#2).
        #   Se Simone (#2) = CANTANDO → GRUPO1 tem Maria/Joao → Proximo = Maria (#3).
        #   Sempre 1→2→3→4→(nova rodada)→1→2→3→4. SEM PULAR.
        next_singer_id_rodada = None
        # Copiar lista e ORDENAR PELA ORDEM VISUAL (arrival_visual_unix) = SAGRADA.
        all_singers_visual_order: list[Singer] = []
        with self._lock:
            all_singers_visual_order = list(self._singers)
        all_singers_visual_order.sort(key=lambda s: s.arrival_visual_unix)

        # Helper local: cantor esta em "GRUPO Ainda Nao Cantou"?
        def _ainda_nao_cantou_rodada(s: Singer) -> bool:
            # (0) CANTANDO AGORA: SEMPRE esta no grupo ainda-nao-cantou (vez dele nao acabou)
            if now_singer_id and s.singer_id == now_singer_id:
                return True
            # (1) demais: status SUNG/SKIPPING/LEFT = ja foi da rodada
            return s.status not in (STATUS_SUNG, STATUS_SKIPPING, STATUS_LEFT_EARLY)

        # ---------- GRUPO 1 (prioridade maxima): Ainda nao cantou nesta rodada ----------
        for s in all_singers_visual_order:
            if now_singer_id and s.singer_id == now_singer_id:
                # Pula o cara que esta cantando AGORA. Proximo vem DEPOIS dele.
                continue
            if _ainda_nao_cantou_rodada(s):
                next_singer_id_rodada = s.singer_id
                break

        # ---------- GRUPO 2 (se GRUPO1 vazio = TODOS ja cantaram de verdade): NOVA RODADA, ORDEM CIRCULAR ==========
        # Para nao pular ninguem na rodada CIRCULAR: quando TODOS sao SUNG (acabou a rodada de verdade e
        # agora comeca nova). ORDEM = JUNIOR (#1) → SIMONE (#2) → MARIA (#3) → JOAO (#4) → JUNIOR (#1)...
        # REGRA CIRCULAR:
        #   (1) PRIMEIRO SUNG que vem DEPOIS do now_singer_id na ORDEM VISUAL (posicao numero MAIOR que now).
        #   (2) Se NAO TEM ninguem depois (now = ultimo da lista): pega PRIMEIRO SUNG do INICIO da lista.
        #   Assim, por exemplo, now = Simone (#2), todos SUNG: proximo = Maria (#3) e nao Junior (#1).
        #   now = Joao (#4), todos SUNG: proximo = Junior (#1, volta pro começo).
        if not next_singer_id_rodada:
            # Descobre o indice do now (na ordem visual, se existir)
            now_visual_idx = None
            for i, s in enumerate(all_singers_visual_order):
                if now_singer_id and s.singer_id == now_singer_id:
                    now_visual_idx = i
                    break
            # (1) Primeiro SUNG DEPOIS do now (indice > now_visual_idx)
            found = None
            for i, s in enumerate(all_singers_visual_order):
                if now_singer_id and s.singer_id == now_singer_id:
                    continue
                if now_visual_idx is not None and i <= now_visual_idx:
                    continue  # soh queremos DEPOIS do now na ordem
                if s.status == STATUS_SUNG:
                    found = s.singer_id
                    break
            if not found:
                # (2) Nao tem ninguem DEPOIS → volta pro começo da lista
                for i, s in enumerate(all_singers_visual_order):
                    if now_singer_id and s.singer_id == now_singer_id:
                        continue
                    if s.status == STATUS_SUNG:
                        found = s.singer_id
                        break
            next_singer_id_rodada = found

        # ---------- Fallback: SKIPPING (caso todo mundo tenha sido pulado mas quer voltar) ----------
        if not next_singer_id_rodada:
            for s in all_singers_visual_order:
                if now_singer_id and s.singer_id == now_singer_id:
                    continue
                if s.status not in (STATUS_LEFT_EARLY,):
                    next_singer_id_rodada = s.singer_id
                    break

        # ---------- Ultimo fallback: next_user da fila real (se bater) ----------
        if not next_singer_id_rodada and next_norm:
            with self._lock:
                for s in self._singers:
                    if self._singer_has_name(s, next_norm):
                        next_singer_id_rodada = s.singer_id
                        break
        if next_singer_id_rodada:
            next_singer_id = next_singer_id_rodada

        unmatched = self.get_unmatched_queue_users(queue_items)
        return {
            "changed": changed,
            "now_singer_id": now_singer_id,
            "next_singer_id": next_singer_id,
            "unmatched": unmatched,
            "now_queue_user": str(now_playing_user or ""),
            "next_queue_user": str(next_user or ""),
        }

    # ------------------------------ persistencia (movida para baixo para API nao quebrar) ------------------------------
    def _load(self) -> None:
        with self._lock:
            if not self._persist_path.exists():
                return
            try:
                raw = json.loads(self._persist_path.read_text(encoding="utf-8"))
            except Exception:
                return
            singers = []
            for item in raw.get("singers", []) or []:
                try:
                    if "arrival_visual_unix" not in item or not float(item.get("arrival_visual_unix") or 0.0):
                        item["arrival_visual_unix"] = float(item.get("arrival_unix") or 0.0)
                    singers.append(Singer(**{k: item[k] for k in item if k in Singer.__dataclass_fields__}))
                except Exception:
                    continue
            singers.sort(key=lambda s: s.arrival_visual_unix)
            self._singers = singers

    def _save(self) -> None:
        with self._lock:
            data = {"saved_at": time.time(), "singers": [asdict(s) for s in self._singers]}
            try:
                self._persist_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            except Exception:
                pass

    # ------------------------------ API publica (basica - manutencao cantor) ------------------------------
    def add_singer(self, name: str, arrival_unix: Optional[float] = None, note: str = "",
                   initial_status: str = STATUS_PENDING_MUSIC) -> Singer:
        """Adiciona cantor. A ordem na exibição respeita REGRA DE RODADA (NO ATO DO INGRESSO):
           - Se já existem cantores que JÁ CANTARAM nesta rodada (SUNG/SKIPPING), o novo
             (que ainda não cantou) SEMPRE fica ANTES deles na lista de exibição.
           - Entre os que ainda não cantaram, a ordem é a de chegada (arrival_unix SAGRADA).
        """
        name = (name or "").strip()
        if not name:
            raise ValueError("Nome do cantor vazio")
        with self._lock:
            arrival = arrival_unix if arrival_unix is not None else time.time()
            singer = Singer(
                singer_id="sg_" + _uuid.uuid4().hex[:10],
                name=name[:80],
                arrival_unix=arrival,
                arrival_visual_unix=arrival,  # REGRA SAGRADA: este valor NUNCA MAIS muda! Ordem visual 1/2/3 nunca muda apos aqui!
                note=(note or "")[:160],
                status=initial_status if initial_status in STATUS_LABEL else STATUS_PENDING_MUSIC,
            )
            self._singers.append(singer)
            # REGRA DO USUARIO: ORDEM VISUAL FIXA = ORDEM DE CHEGADA (arrival_visual_unix).
            # O arrival_unix PODE mudar depois (logica de rodada, proximo cantor), mas NAO afeta a ordem dos cards.
            self._singers.sort(key=lambda s: s.arrival_visual_unix)
            self._save()
            return singer

    def update_status(self, singer_id: str, new_status: str) -> Optional[Singer]:
        """Atualiza status. NÃO MOVE NINGUÉM VISUALMENTE (ordem visual congelada).
        Apenas atualiza arrival_unix para futuro (proxima rodada) se SUNG/SKIPPING
        e atualiza contagem rounds_sung. A ordem de self._singers NUNCA muda visualmente."""
        if new_status not in STATUS_LABEL:
            return None
        with self._lock:
            idx = next((i for i, s in enumerate(self._singers) if s.singer_id == singer_id), None)
            if idx is None:
                return None
            singer = self._singers[idx]
            old_status = singer.status
            singer.status = new_status
            singer.updated_at_unix = time.time()
            # REGRA DE RODADA: SUNG/SKIPPING -> atualiza arrival_unix para AGORA+1ms.
            # Isto NAO MOVE ele VISUALMENTE (pois nao fazemos pop/append aqui),
            # mas a logica de CALCULO DO PROXIMO CANTOR (_reordered_by_rodada) usa
            # o novo arrival_unix para por ele ULTIMO no GRUPO2 (ordem logica proxima rodada).
            # A lista self._singers mantem a MESMA posicao de indice de todos os cantores.
            if new_status == STATUS_SKIPPING and old_status != STATUS_SKIPPING:
                singer.arrival_unix = time.time() + 0.001
            if new_status == STATUS_SUNG and old_status != STATUS_SUNG:
                singer.rounds_sung += 1
                singer.arrival_unix = time.time() + 0.001
            # === NÃO MOVE NINGUÉM VISUALMENTE ===
            # NÃO faz self._singers = self._reordered_by_rodada(...)
            # NÃO faz self._singers.sort(...)
            # NÃO faz pop/append.
            self._save()
            return singer

    def update_note(self, singer_id: str, note: str) -> Optional[Singer]:
        with self._lock:
            for s in self._singers:
                if s.singer_id == singer_id:
                    s.note = (note or "")[:160]
                    s.updated_at_unix = time.time()
                    self._save()
                    return s
            return None

    def bump_songs_added(self, name: str) -> Optional[Singer]:
        """FASE 2: Quando usuario adicionar musica na fila real pelo QR/botao.
        Incrementa songs_added_total. Nao cria cantor automatico (operador que controla lista)."""
        name = (name or "").strip()
        if not name:
            return None
        with self._lock:
            singer = None
            n = _normalize_name(name)
            if n and n not in IGNORED_SYSTEM_USERS:
                for s in self._singers:
                    if self._singer_has_name(s, n):
                        singer = s
                        break
            if singer is not None:
                singer.songs_added_total += 1
                singer.updated_at_unix = time.time()
                if singer.status == STATUS_PENDING_MUSIC:
                    singer.status = STATUS_WAITING
                self._save()
            return singer

    def remove_singer(self, singer_id: str) -> bool:
        with self._lock:
            n = len(self._singers)
            self._singers = [s for s in self._singers if s.singer_id != singer_id]
            if len(self._singers) != n:
                self._save()
                return True
            return False

    def clear_all(self) -> None:
        with self._lock:
            self._singers = []
            self._save()

    def get_pending_calls(self) -> list[Singer]:
        """Retorna cantores com STATUS_PENDING_MUSIC (chegaram, não colocaram música -> CHAMAR!)"""
        with self._lock:
            return [s for s in self._singers if s.status == STATUS_PENDING_MUSIC]
