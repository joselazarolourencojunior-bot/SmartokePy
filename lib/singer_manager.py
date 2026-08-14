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
import logging
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
    # ========================================================================
    # [LEI DA VEZ SAGRADA - CAMPOS V27 - AGOSTO 2026]
    # Motivo: antes, um clique errado em "🔔 Sem música" no cantor que JÁ
    # CANTOU (SUNG) resetava status para PENDING_MUSIC, e ele voltava pro
    # INICIO da fila de "ainda não cantou" → BUG clássico: Junior já cantou
    # (rodada 1) volta para a vez passando na frente de Simone (#2 visual)
    # que ainda não cantou NENHUMA vez.
    #
    # Solução: a informação de "ele já teve sua vez NA RODADA ATUAL NÚMERO N"
    # fica DESANEXADA do status atual. Mesmo que status mude (Sem música,
    # Tem música, etc.) o "teve_vez_nesta_rodada" só é False na RODADA 1 para
    # quem ainda não foi. Quando o cantor acaba a música de verdade (SUNG)
    # OU clica em "Pular rodada" (SKIPPING) OU "Saiu cedo" (LEFT_EARLY),
    # ele marca teve_vez_nesta_rodada = True e SÓ VOLTA pra False quando
    # TODOS da lista forem True = COMEÇA A RODADA 2. Regra de 1ª 2ª 3ª vez
    # por rodada garante ordem 1→2→3→4 para SEMPRE.
    # ========================================================================
    teve_vez_nesta_rodada: bool = False   # True = ele já foi atendido na rodada atual
    rodada_que_teve_vez: int = 0          # número da rodada que ele teve a vez (0 = nunca)
    rodada_atual_sistema: int = 1         # sincronizado com o rodada_atual do Manager
    # ========================================================================
    # [LEI DA VEZ SAGRADA V41 - CORRECAO FINAL 2026-08-13 - MANTEM ORDEM FIXA
    #  DURANTE TODA A RODADA, NAO MUDANDO NO MEIO]
    # Motivo Bug V38 identificado em 13/08 15:40:
    #   - RODADA 3697 comecou com todos rounds_sung: Junior 3 / Simone 2 /
    #     Jose das Coves 1 / Zequinha 2
    #   - V38 NO INICIO DA RODADA escolheu Jose 1 (menor) PRIMEIRO — PERFEITO.
    #   - Ele comecou a musica e ESTA CANTANDO AGORA (meio da rodada).
    #   - Problema: quando a musica ACABAR (fim, 2:30s), rounds_sung dele vira 2
    #     (de 1 para 2) → se A CADA refresh recalculasse ordem do GRUPO1 por
    #     rounds_sung ASC, agora Jose 2 / Simone 2 / Zequinha 2 → ordenação
    #     quebra, ele poderia voltar para o MEIO da linha ANTES de Simone ou
    #     Zequinha, pular eles ou passar (bug que usuario reclamou).
    #
    # SOLUCAO V41:
    #   - A ORDEM DE CHAMADA DA RODADA SÓ É CALCULADA 1 ÚNICA VEZ, NO
    #     EXATO MOMENTO QUE A RODADA COMEÇA (primeiro refresh/cálculo depois
    #     de nova rodada ou SET vazio / ninguém com teve_vez_nesta_rodada=True).
    #   - Nesse momento, criamos uma lista ORDENADA com todos os ativos por
    #     (rounds_sung ASC, arrival ASC — regra V38 que o usuario pediu), e
    #     para cada cantor GRAVAMOS UMA POSICAO FIXA (1,2,3,4) NESTES CAMPOS.
    #   - DURANTE O RESTO TODO DA RODADA, QUALQUER cálculo de próximo do
    #     GRUPO1 usa SOMENTE `ordem_chamada_na_rodada ASC`. NÃO OLHAMOS MAIS
    #     rounds_sung NEM arrival para escolher ordem no meio da rodada.
    #   - Isso garante: SE no inicio da rodada foi definido 1=Jose,
    #     2=Simone, 3=Zequinha, 4=Junior → MESMO que Jose acabe sua música e
    #     aumente rounds_sung de 1→2, os restantes CONTINUAM Simone→Zequinha
    #     →Junior, ninguém pula ninguém, EXATAMENTE como o usuario pediu.
    #   - Na PROXIMA RODADA (todos passaram), resetamos estes campos para
    #     0, entao re-calculamos a ordem com os rounds_sung NOVOS (agora
    #     Jose tem 2, Simone pode ter 2 ou 3 etc.) e assim sucessivamente.
    # ========================================================================
    ordem_chamada_na_rodada: int = 0      # 1..N dentro da rodada atual (0 = ainda nao foi atribuida nesta rodada)
    rodada_que_recebeu_ordem: int = 0     # qual rodada ele recebeu esta ordem (0 = nunca / invalida agora, usar para garantir que ordem antiga nao vaza pra proxima)
    status_manualmente_marcado_at_unix: float = 0.0   # [BUG V43 TARJA] quando operador clica nos botoes "✅ Tem musica" ou "🔔 Sem musica" do card, GRAVA a hora aqui. Sync V34 NAO SOBRESCREVE status ate 2 min depois do clique (120s).


class SingerManager:
    """Gerenciador thread-safe de lista de cantores (ordem de chegada sagrada)."""

    def __init__(self, persist_path: Optional[str] = None) -> None:
        self._lock = threading.RLock()
        self._singers: list[Singer] = []
        # [LEI DA VEZ SAGRADA V27]: número da rodada GERAL que o sistema está AGORA.
        # 1 = primeira rodada (todos ainda não cantaram). Quando TODOS forem
        # atendidos (teve_vez_nesta_rodada=True) → incrementa para 2 e zera todos.
        # [LEI DA VEZ SAGRADA V31 - AGORA É PERSISTIDO NO JSON!]: Antes no restart
        # do service voltava sempre para 1, quebrando fallbacks de rodada >= 2.
        self._rodada_atual_geral: int = 1
        # [LEI DA VEZ SAGRADA V31 - SET INFALÍVEL DE PASSARAM NESTA RODADA]
        # Contém os singer_ids dos cantores que JÁ PASSARAM (tiveram sua vez) na
        # RODADA ATUAL. É a camada MAIS ALTA de todas — NUNCA é apagada por
        # mudança de status, nunca é apagada por clique em botão "Sem música".
        # Só é apagada (zerada) quando UMA NOVA RODADA começa (todos passaram).
        # É SALVO no JSON de persistência e RECARREGADO no _load().
        # Essa é a garantia MATEMÁTICA que NUNCA MAIS Junior volta na frente
        # de Simone depois que teve sua vez — independente de qualquer coisa.
        self._singers_passaram_nesta_rodada_ids: set[str] = set()
        if persist_path is None:
            try:
                from pikaraoke.lib.get_platform import get_data_directory

                base = Path(get_data_directory())
            except Exception:
                base = Path.home() / ".pikaraoke"
            base.mkdir(parents=True, exist_ok=True)
            persist_path = str(base / "singers_state_fase1.json")
        self._persist_path = Path(persist_path)
        # [LEI VEZ SAGRADA V30 - LOG OBRIGATORIO DO CAMINHO]
        # Nunca mais fique no escuro de ONDE o estado esta sendo salvo.
        try:
            logging.info(
                "[VezSagradaV31] SingerManager inicializado. "
                "persist_path = %s | existe_arquivo_agora? %s | tamanho=%s bytes",
                str(self._persist_path),
                str(self._persist_path.exists()),
                str(self._persist_path.stat().st_size if self._persist_path.exists() else "-1"),
            )
        except Exception:
            pass
        self._load()

    @staticmethod
    def _normalize_name(text: str) -> str:
        # wrapper estatico para _normalize_name (evitar circular; ja existe global)
        return _normalize_name(text)

    # ------------------------------------------------------------------
    # [LEI DA VEZ SAGRADA V27 - verifica baseado em teve_vez_nesta_rodada]
    # NÃO USA MAIS status SUNG para decidir se o cantor já foi na rodada.
    # Motivo: antes clicar "Sem música" resetava status→PEND_MUSIC e voltava
    # o cantor que já cantou para GRUPO1. Agora teve_vez_nesta_rodada é a
    # VERDADE ÚNICA.
    # ------------------------------------------------------------------
    def _singer_ja_teve_vez_na_rodada_atual(self, singer: Singer) -> bool:
        # (0) [CAMADA MAIS ALTA V31 - SET INFALIVEL]
        # Se o singer_id está no SET de passaram → JÁ PASSOU. Ponto final.
        # Esta camada NUNCA depende de status, nunca depende de contador, nunca
        # depende de campo do dataclass — só do SET que só é modificado quando
        # realmente a vez acontece, e só é zerado quando NOVA RODADA começa.
        try:
            if str(singer.singer_id) in self._singers_passaram_nesta_rodada_ids:
                return True
        except Exception:
            pass
        # (1) já está marcado como "teve vez" nesta rodada? (SUNG/SKIP/LEFT)
        if singer.teve_vez_nesta_rodada and singer.rodada_atual_sistema == self._rodada_atual_geral:
            return True
        # (2) LEFT_EARLY = NUNCA mais quer cantar → sempre "já foi" (nunca mais proximo)
        if singer.status == STATUS_LEFT_EARLY:
            return True
        # [LEI VEZ SAGRADA V28 - FALLBACK ANTIGO - BUG RAIZ 13/08/2026]
        if singer.status in (STATUS_SUNG, STATUS_SKIPPING):
            return True
        # [LEI VEZ SAGRADA V29 - FALLBACK ABSOLUTO COM rounds_sung]
        if int(getattr(singer, "rounds_sung", 0) or 0) > 0:
            if int(self._rodada_atual_geral or 1) <= (int(getattr(singer, "rounds_sung", 0) or 0) + 1):
                if int(getattr(singer, "rounds_sung", 0) or 0) >= int(self._rodada_atual_geral or 1):
                    return True
                if int(getattr(singer, "rodada_que_teve_vez", 0) or 0) == int(self._rodada_atual_geral or 1):
                    return True
        return False

    @staticmethod
    def _is_status_already_in_rodada(singer: Singer) -> bool:
        """Retorna True se o cantor JA FOI processado nesta rodada (usa teve_vez_nesta_rodada + status).
        Ou seja: já teve vez, ou pulou, ou saiu cedo.
        """
        if singer.status == STATUS_LEFT_EARLY:
            return True
        if singer.teve_vez_nesta_rodada:
            return True
        # fallback compatibilidade: status SUNG / SKIPPING / LEFT também contam
        # (caso o estado venha de uma versão antiga sem os campos novos).
        return singer.status in (STATUS_SUNG, STATUS_SKIPPING, STATUS_LEFT_EARLY)

    def _marcar_teve_vez_na_rodada(self, singer: Singer) -> None:
        """Marca que este cantor JÁ FOI ATENDIDO nesta rodada (não volta para GRUPO1 até nova rodada)."""
        if singer is None:
            return
        singer.teve_vez_nesta_rodada = True
        singer.rodada_que_teve_vez = int(self._rodada_atual_geral)
        singer.rodada_atual_sistema = int(self._rodada_atual_geral)
        singer.updated_at_unix = time.time()
        # [LEI VEZ SAGRADA V31 - ADICIONA NO SET INFALÍVEL]
        # Esta é a garantia final: mesmo que depois apaguem teve_vez_nesta_rodada,
        # mesmo que voltem status para WAITING/PENDING por erro operador, o SET
        # contém o ID e a próxima verificação camada 0 (primeira linha do helper)
        # retorna True sempre → ele não volta mais para GRUPO1 nesta rodada.
        try:
            if singer.singer_id:
                self._singers_passaram_nesta_rodada_ids.add(str(singer.singer_id))
        except Exception:
            pass

    def _verificar_inicio_nova_rodada_e_reinicializar(self) -> bool:
        """Chamado NO FIM de refresh_statuses_from_queue.
        Retorna True se começou uma nova rodada (todos foram atendidos).
        Quando todos os cantores ativos (não LEFT_EARLY) já tiveram
        teve_vez_nesta_rodada=True (ou SKIPPING / LEFT_EARLY) → incrementa
        _rodada_atual_geral + zera teve_vez=False para TODOS → COMEÇA RODADA 2.
        """
        with self._lock:
            todos = list(self._singers)
        if not todos:
            return False
        # cantores "ativos" = não LEFT_EARLY
        ativos = [s for s in todos if s.status != STATUS_LEFT_EARLY]
        if not ativos:
            return False
        # Quantos ativos JÁ tiveram vez? (NÃO PODEMOS marcar ninguém aqui!)
        qtd_ja_teve_vez = 0
        for s in ativos:
            if self._singer_ja_teve_vez_na_rodada_atual(s):
                qtd_ja_teve_vez += 1
            # Fallback: status SUNG / SKIPPING são "já passou de vez" garantidos
            elif s.status in (STATUS_SUNG, STATUS_SKIPPING):
                # Se caiu aqui = é pq helper disse "não passou" mas o status é definitivo.
                # Marcar "teve vez" AGORA para essa rodada, garantindo que ele conta.
                with self._lock:
                    self._marcar_teve_vez_na_rodada(s)
                qtd_ja_teve_vez += 1
        # [REGRA DURA V33 / V35: TODOS os ativos passaram? ENTÃO COMEÇAR NOVA RODADA!]
        # [BUG V35 13/08 14:12 - CORREÇÃO FINAL DEFINTIVA]
        # Antes V33/V34: apenas checkava qtd_ja_teve_vez >= len(ativos).
        #   Problema: Zequinha na rodada 1 estava SEM MÚSICA (status PENDING_MUSIC)
        #   mas o helper _singer_ja_teve_vez disse "ele não passou" (errado! ele
        #   realmente não passou!). -> qtd_ja_teve_vez = 3 (apenas Junior/Simone/Jose),
        #   mas caiu no fallback "status SUNG? sim (voltou JSON!)" → marca
        #   teve_vez AGORA no Zequinha, então qtd >= ativos → INICIA RODADA 2,
        #   Junior é o primeiro novamente, na FRENTE de todo mundo.
        #
        # CRITERIO V35 INFALÍVEL para iniciar RODADA NOVA:
        #   (A) qtd_ja_teve_vez >= len(ativos)   → 100% dos ativos já foram.
        #         E TAMBÉM
        #   (B) GRUPO 1 ("ainda não cantou NESTA RODADA") ESTIVER VAZIO.
        #
        # Se qualquer um do GRUPO 1 ainda existe (alguém com teve_vez=False,
        # mesmo se status estiver WAITING ou PENDING_MUSIC) → NÃO É HORA DE
        # RODADA 2! Tem gente na frente para ser chamado.
        grupo1_ainda_existe = False
        for s in ativos:
            if not self._singer_ja_teve_vez_na_rodada_atual(s):
                grupo1_ainda_existe = True
                break
        if qtd_ja_teve_vez >= len(ativos) and len(ativos) > 0 and (not grupo1_ainda_existe):
            # 🔔 NOVA RODADA DETECTADA!
            self._rodada_atual_geral = int(self._rodada_atual_geral) + 1
            # [LEI VEZ SAGRADA V31 - ZERA O SET INFALÍVEL quando muda de rodada]
            try:
                self._singers_passaram_nesta_rodada_ids.clear()
            except Exception:
                pass
            with self._lock:
                # [BUG CRITICO V34 CORRECAO 13/08 14:10]
                # Quando RODADA 2 começa, se NÃO resetarmos o status SUNG → os
                # cantores permanecem SUNG indefinidamente. O bloco de sync status
                # (refresh_statuses_from_queue linhas 694 e 705) nao sobreescreve
                # SUNG → ficam pra sempre "SUNG", sem tarjas, sem nunca mais tocar.
                #
                # SOLUCAO V34: Toda vez que uma nova rodada é criada, o status
                # dos cantores (antes SUNG ou SKIPPING) é RESETADO para:
                #  - Se tem MÚSICA NA FILA REAL (queue_users_norm recebido? neste
                #    ponto não temos, mas damos um default SAFE de PENDING_MUSIC —
                #    pois o próximo refresh_statuses_from_queue vai logo a seguir
                #    e seta para WAITING se tiver música na fila).
                #  - Se LEFT_EARLY → continua LEFT_EARLY (nunca mexe).
                for s in todos:
                    s.teve_vez_nesta_rodada = False
                    s.rodada_atual_sistema = int(self._rodada_atual_geral)
                    # [V34 RESET DE STATUS EM NOVA RODADA]
                    if s.status == STATUS_SUNG or s.status == STATUS_SKIPPING:
                        # Resetamos para PENDING_MUSIC (SEM MÚSICA AINDA) — na
                        # proxima iteracao de refresh, se tiver musica na fila,
                        # vira WAITING. Assim garantimos que SUNG nao trava
                        # para sempre e a rodada 2 começa com todos live.
                        s.status = STATUS_PENDING_MUSIC
                        s.updated_at_unix = time.time()
                    # [V41 CORRECAO FINAL - RESET DA ORDEM CHAMADA EM RODADA NOVA!]
                    # Como uma nova rodada começou, zeramos "ordem_chamada_na_rodada"
                    # e "rodada_que_recebeu_ordem" em TODOS. O proximo helper
                    # _garantir_ordem_chamada_rodada_atual_definida vai
                    # recalcular a ordem usando (rounds_sung ASC, arrival ASC)
                    # com os valores ATUALIZADOS (ex: Jose ja cantou 2x agora,
                    # nao 1x mais) e assim Simone/Junior/Zequinha sao
                    # ordenados corretamente no inicio da rodada nova, mas
                    # DURANTE ESSA RODADA NOVA a ordem permanece FIXA.
                    s.ordem_chamada_na_rodada = 0
                    s.rodada_que_recebeu_ordem = 0
                self._save()
            try:
                logging.warning(
                    "[VezSagradaV33] 🔔 NOVA RODADA detectada! Todos %s/%s cantores ativos foram atendidos → agora RODADA #%s. SET infalivel zerado, teve_vez_nesta_rodada=False em TODOS.",
                    str(qtd_ja_teve_vez),
                    str(len(ativos)),
                    str(self._rodada_atual_geral),
                )
            except Exception:
                pass
            return True
        return False

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

    def _garantir_ordem_chamada_rodada_atual_definida_se_necessario(self, all_singers_visual_order: list) -> None:
        """[LEI VEZ SAGRADA V43 - LEI ABSOLUTA E DEFINITIVA - NUNCA MAIS MEXER EM ORDEM]

        *********************************************************************
        *  REGRA ÚNICA, ABSOLUTA, INQUEBRÁVEL:                              *
        *                                                                   *
        *  A ORDEM DE CHAMADA É SEMPRE E EXCLUSIVAMENTE A SEQUÊNCIA VISUAL  *
        *  DOS CARDS, NA ORDEM DE CHEGADA, CIRCULARMENTE.                   *
        *                                                                   *
        *  #1 visual → #2 visual → #3 visual → #4 visual → #1 visual ...   *
        *                                                                   *
        *  PONTO FINAL. NÃO EXISTEM EXCEÇÕES.                               *
        *                                                                   *
        *  rounds_sung NÃO INTERFERE NA ORDEM, NUNCA, EM HIPÓTESE NENHUMA.  *
        *  rounds_sung é APENAS UMA ESTATÍSTICA DE HISTÓRICO (contagem      *
        *  de quantas músicas a pessoa já cantou na vida). NADA MAIS.       *
        *                                                                   *
        *  Ninguém pula ninguém. Ninguém "tem prioridade" por ter cantado   *
        *  menos. A justiça é a ordem de chegada da pessoa no sistema.      *
        *  NÃO ADIANTA adicionar música, NÃO ADIANTA clicar em nada,        *
        *  A FILA SAGRADA OBDECE A ORDEM DE CHEGADA E SÓ A ELA.             *
        *********************************************************************

        Chama isso 1x NO INÍCIO de get_next_singer_that_should_sing_now() E de
        refresh_statuses_from_queue().

        O QUE FAZ (NÃO FAZ MAIS DE 1 VEZ POR RODADA):
          1. Verifica SE A ORDEM DESTA RODADA JÁ ESTÁ DEFINIDA:
               - EXISTE pelo menos 1 cantor ativo onde:
                 `rodada_que_recebeu_ordem == _rodada_atual_geral`
               - Se sim → ordem já definida no começo da rodada → SAI SEM FAZER NADA.
          2. SE A ORDEM AINDA NÃO EXISTE (começo da rodada, SET vazio, ninguém teve vez):
               A ORDEM É ALL_SINGERS_VISUAL_ORDER (que já está ordenado por
               arrival_visual_unix ASC). Isto é: #1, #2, #3, #4 sempre.
               - Atribui `ordem_chamada_na_rodada = pos+1 (1, 2, 3, 4)` a cada um.
               - Grava WARNING no journal.
          3. EDGE CASE RODADA JÁ EM ANDAMENTO (deploy/restart JSON antigo):
               - Mesma coisa, mas mantemos os que já passaram no começo e os
                 restantes são ordenados por chegada. Nenhuma surpresa.

        Args:
            all_singers_visual_order: lista já ordenada por arrival_visual_unix.
        """
        if not all_singers_visual_order:
            return
        _rodada = int(self._rodada_atual_geral or 1)
        # 1) Verificar: alguém já tem ordem válida para rodada atual?
        tem_ordem_ja = False
        tem_algum_teve_vez = False
        ativos: list = []
        for s in all_singers_visual_order:
            if s.status == STATUS_LEFT_EARLY:
                continue
            ativos.append(s)
            if int(getattr(s, "rodada_que_recebeu_ordem", 0) or 0) == _rodada and \
               int(getattr(s, "ordem_chamada_na_rodada", 0) or 0) > 0:
                tem_ordem_ja = True
            if self._singer_ja_teve_vez_na_rodada_atual(s):
                tem_algum_teve_vez = True
        if not ativos:
            return
        # Se já tem ordem gravada pra esta rodada, NAO GERA DE NOVO (regra V41 mantida!)
        if tem_ordem_ja:
            return
        # ===================== V43 - LEI ABSOLUTA =====================
        # CASO BEM SIMPLES: SEMPRE ordem visual. Menor, mais seguro, MENOS BUGS.
        # Tanto começo limpo de rodada quanto meio de rodada (fallback):
        # sempre ordem visual = ativos (que já são all_singers_visual_order
        # filtrados só por LEFT_EARLY, mantendo a ordem de chegada intacta).
        # NÃO OLHAMOS MAIS PARA rounds_sung NESTE ARQUIVO, NUNCA MAIS.
        ordem_final = list(ativos)
        # =================================================================
        # Agora grava a ordem em CADA cantor desta rodada:
        mudou = 0
        with self._lock:
            for pos, s in enumerate(ordem_final):
                ordem_nova = int(pos + 1)
                antiga_rod = int(getattr(s, "rodada_que_recebeu_ordem", 0) or 0)
                antiga_ord = int(getattr(s, "ordem_chamada_na_rodada", 0) or 0)
                if antiga_rod != _rodada or antiga_ord != ordem_nova:
                    s.ordem_chamada_na_rodada = ordem_nova
                    s.rodada_que_recebeu_ordem = _rodada
                    s.updated_at_unix = time.time()
                    mudou += 1
            if mudou:
                try:
                    self._save()
                except Exception:
                    pass
        # Log forte no journal para o usuário ler a ordem definida (exatamente como na tela)
        try:
            desc = []
            for s in ordem_final:
                desc.append(
                    f"#{int(getattr(s,'ordem_chamada_na_rodada',0))} "
                    f"{s.name} "
                    f"(chegada=#{[i for i,x in enumerate(all_singers_visual_order,1) if x.singer_id==s.singer_id][0] if any(x.singer_id==s.singer_id for x in all_singers_visual_order) else '?'})"
                )
            motivo = "INICIO RODADA (vazia, limpa)" if not tem_algum_teve_vez else "MEIO RODADA (JSON antigo/deploy, fallback ordem chegada)"
            logging.warning(
                "[V43 LEI ABSOLUTA] ORDEM RODADA #%s DEFINIDA 1X E FIXA AGORA (SEM ROUNDS_SUNG, SO ORDEM CHEGADA VISUAL) - %s] %s",
                str(_rodada),
                motivo,
                "  →  ".join(desc),
            )
        except Exception:
            pass
        return

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
        """FASE 2 AUTOMACAO 1: Chamado quando musica ACABOU (song_ended)
        OU quando playback detectou que is_playing=False ANTES do evento
        song_ended chegar (RACE CONDITION V50).

        Pega o user da musica que acabou / estava tocando -> marca cantor como SUNG
        (acrescenta rounds_sung + 1, move para o FIM da rodada, marca teve_vez=True
        nas 3 camadas). Se o cantor JA tinha teve_vez=True, nao faz nada (idempotente).
        Nao duplica contagem, nao duplica SET infalivel.
        """
        s = self.find_singer_by_queue_user(queue_user)
        if s is None:
            return None
        # [V50 IDEMPOTENCIA] Se ja marcou teve_vez nesta rodada, retorna rapido.
        if self._singer_ja_teve_vez_na_rodada_atual(s):
            logging.info(
                "[V50 mark_singer_sung IDEMPOTENTE OK] user_fila=%r singer=%r "
                "JA TEM teve_vez_nesta_rodada=True (ja foi marcado antes, race "
                "condition evitada). Nao duplica rounds_sung.",
                str(queue_user), getattr(s, "name", "")
            )
            return s
        return self.update_status(s.singer_id, STATUS_SUNG)

    def mark_teve_vez_RACE_QUICK_by_queue_user(self, queue_user: str, reason: str = "race-quick") -> Optional[Singer]:
        """[V50 CORRECAO RACE CONDITION FIM-DE-MUSICA]

        Chamado pelo Run Loop do karaoke.py NO EXATO MOMENTO EM QUE detecta
        `not is_playing` e ANTES do reset_now_playing(). Serve para PRE-MARCAR
        o cantor que acabou de tocar como "teve_vez_nesta_rodada=True" nas 3
        camadas de protecao (bool, rodada_que_teve_vez, SET infalivel),
        ANTES que o Run Loop faca a proxima chamada a get_next_singer_that_should_sing_now().

        Isso evita o bug do Junior tocar 2x seguidas: a marcação do teve_vez
        acontecia 20-50ms DEPOIS (no evento song_ended, callback) mas o Run
        Loop detectava is_playing=False ANTES e pegava Junior de novo como
        proximo, pois teve_vez ainda era False.

        DIFERENCA para mark_singer_sung_by_queue_user (usado no fim com motivo complete):
            - NAO incrementa rounds_sung.
            - NAO marca status=SUNG (fica em pending_music/waiting, o song_ended faz isso depois).
            - MARCA SOMENTE as 3 camadas de "ja passou nesta rodada"
              (teve_vez_nesta_rodada, rodada_que_teve_vez, SET infalivel).
            - IDEMPOTENTE: se ja teve vez, nao faz nada.
            - Retorna o Singer que marcou, ou None se nao encontrou.
        """
        if not queue_user:
            return None
        s = self.find_singer_by_queue_user(queue_user)
        if s is None:
            return None
        # Idempotencia
        if self._singer_ja_teve_vez_na_rodada_atual(s):
            logging.info(
                "[V50 mark_teve_vez_RACE_QUICK IDEMPOTENTE OK] "
                "user_fila=%r singer=%r ja estava marcado teve_vez=True "
                "motivo=%r. Nao faz nada.",
                str(queue_user), getattr(s, "name", ""), str(reason)
            )
            return s
        # Marca as 3 camadas (igual ao helper _marca_cantor_ja_passou)
        with self._lock:
            s.teve_vez_nesta_rodada = True
            s.rodada_que_teve_vez = int(self._rodada_atual_geral or 1)
            s.rodada_atual_sistema = int(self._rodada_atual_geral or 1)
            s.updated_at_unix = float(time.time())
            try:
                if hasattr(self, "_singers_passaram_nesta_rodada_ids"):
                    if self._singers_passaram_nesta_rodada_ids is None:
                        self._singers_passaram_nesta_rodada_ids = set()
                    self._singers_passaram_nesta_rodada_ids.add(str(s.singer_id))
            except Exception:
                pass
            try:
                self._save()
            except Exception:
                pass
        logging.warning(
            "[V50 RACE-QUICK MARCADO TEVE_VEZ] user_fila=%r singer=%r id=%r. "
            "Marcou teve_vez_nesta_rodada=True + SET infalivel antes de reset_now_playing "
            "(motivo=%r). Assim o proximo get_next NAO retorna ele de novo, "
            "mesmo que o evento song_ended demore 50ms para chegar e marcar SUNG.",
            str(queue_user), getattr(s, "name", ""), getattr(s, "singer_id", "?"),
            str(reason)
        )
        return s

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
        """LEI ABSOLUTA V27 (SETEMBRO 2026): retorna QUEM é o PRÓXIMO cantor OBRIGATÓRIO na
        ORDEM VISUAL SAGRADA 1→2→3→4.

        [MUDANÇA MAIS CRÍTICA V27]:
          NÃO USA MAIS o status SUNG/SKIPPING/LEFT_EARLY para decidir se o cantor já
          foi na rodada. AGORA USA o campo `teve_vez_nesta_rodada` (BOLEAN +
          rodada_atual_sistema), que é SETADO apenas quando:
            (a) cantor ACABA a música de verdade (SUNG no fim),
            (b) operador clica "Pular rodada" (SKIPPING),
            (c) "Saiu cedo" (LEFT_EARLY).
          Cliques em "🔔 Sem música" NÃO APAGAM esta marca.
          Isso resolve o bug clássico: Junior já cantou, alguém clica Sem música
          nele → status vira PENDING_MUSIC → ele voltava para a FRENTE da Simone
          (#2 visual) que ainda não tinha tido nenhuma vez.

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

            # ===== [V51 CORRECAO BUG A - NOVA RODADA DETECTAR ANTES DE CALCULAR PROXIMO] =====
            # O Run Loop chama get_next() para escolher a proxima musica ANTES de
            # rodar refresh_statuses_from_queue que é quem normalmente dispara
            # _verificar_inicio_nova_rodada_e_reinicializar(). Então se a rodada
            # acabou de fechar (todos passaram), o sistema ainda está com
            # teve_vez=True de todos da rodada antiga, GRUPO1 fica vazio, e cai no
            # GRUPO2 circular que escolhe "o proximo depois do ultimo" = Simone (#2)
            # ao invés de JÚNIOR (#1, primeiro da NOVA RODADA por chegada).
            #
            # EXEMPLO BUG QUE O USUARIO REPORTOU:
            #   Rodada 1: Junior #1 cantou (teve_vez=True), Simone #2 pulou
            #   (teve_vez=True), Jose #3 pulou, Zequinha #4 pulou. Todos passaram.
            #   Run Loop detectou "nao esta tocando" (fim da musica do Junior).
            #   ANTES da V51: get_next() rodou SEM refresh, nao houve nova rodada.
            #   GRUPO1 = VAZIO (todos teve_vez=True). Cai no GRUPO2 circular.
            #   Now = Junior (#0 visual). Proximo depois dele = Simone (#1 visual).
            #   Retorna Simone. Junior fica com a 2a musica parada. Usuario
            #   reclamou: "nao chegou em Junior, voltou em Simone nao pode".
            #
            # CORRECAO V51: Chamar _verificar_inicio_nova_rodada_e_reinicializar()
            # AQUI DENTRO, ANTES de qualquer cálculo, para zerar tudo e criar a
            # rodada nova se for o caso. Depois prosseguir normalmente com a
            # ordem nova. A funcao é 100% idempotente: se nao for hora, retorna
            # False, nada muda, nao quebra nada.
            _rodada_iniciou_agora_mesmo = False
            try:
                _rodada_iniciou_agora_mesmo = self._verificar_inicio_nova_rodada_e_reinicializar()
            except Exception as exc_v51:
                logging.warning("[V51] _verificar_inicio_nova_rodada dentro de get_next falhou (ignorado): %s", exc_v51)
                _rodada_iniciou_agora_mesmo = False
            if _rodada_iniciou_agora_mesmo:
                # Atualiza a referência dos objetos (nao precisa remontar a lista,
                # os objetos apontam pros mesmos endereços).
                logging.warning(
                    "[V51 BUG A 100% FIXADO] Nova rodada #%s detectada DENTRO DE get_next() (antes do calculo proximo). "
                    "Anteriormente o sistema cairia no GRUPO2 circular e escolheria o #2 (Simone ou proximo depois do ultimo). "
                    "Agora comeca limpo: ordem_chamada = 1,2,3,4 = chegada visual (#1 JUNIOR primeiro, depois #2 Simone, #3 Jose, #4 Zequinha). "
                    "Junior volta para o topo com sua 2a musica.",
                    str(int(self._rodada_atual_geral or 1))
                )
            # ==================================================================================

            # ===== [V41 CORRECAO FINAL - PASSO 1] =====
            # ANTES de qualquer cálculo de GRUPO1, definir a ORDEM DA RODADA
            # se ainda não estiver definida (1 vez por rodada, no começo).
            # Depois disso, TODOS os sort do GRUPO1 usam ordem_chamada_na_rodada ASC
            # e NÃO MAIS recalculam rounds_sung a cada refresh (evita pulos no meio
            # da rodada quando alguém acaba de cantar e aumenta rounds_sung).
            self._garantir_ordem_chamada_rodada_atual_definida_se_necessario(all_singers_visual_order)

            now_singer_id: str | None = None
            if now_playing_user:
                s_now = self.find_singer_by_queue_user(str(now_playing_user))
                if s_now is not None:
                    now_singer_id = s_now.singer_id
            # [V44 BUG FINAL 16:21 - JUNIOR VOLTOU 2X SEGUIDAS]
            # Correcao SUPREMA: O now_singer_id (quem está tocando agora) só tem
            # "imunidade de exclusao temporaria" APENAS se ele AINDA NAO teve vez
            # na rodada (ou seja: ele comecou a tocar agora e esta la na tela CANTANDO
            # AGORA e NATURALMENTE nao pode ser o "proximo" ao mesmo tempo).
            # SE ELE JA TEVE VEZ (teve_vez_nesta_rodada=True, por exemplo acabou
            # de acabar a musica e now_playing_user ainda nao foi zerado pelo
            # playback_controller (race condition de 50ms), entao ELE TEM QUE SAIR
            # do GRUPO1 imediatamente, igual todo mundo que ja passou.
            # ANTES (V43 bug): _ainda_nao_cantou(Junior) retornava True so por
            # ser now_singer_id, mas ao mesmo tempo ele era SKIPADO do grupo1
            # (if now_singer_id... continue) → GRUPO1 FICAVA VAZIO e o GRUPO2
            # (circular) recomecava no proximo depois de Junior = o Junior de novo,
            # e tocava 2x seguidas e Simone nunca tinha vez.
            now_singer_ja_teve_vez = False
            if now_singer_id:
                sn = _by_id.get(str(now_singer_id))
                if sn is not None:
                    now_singer_ja_teve_vez = self._singer_ja_teve_vez_na_rodada_atual(sn)

            # ===== HELPER LOCAL - V27 - USA teve_vez_nesta_rodada =====
            def _ainda_nao_cantou_rodada_v27(s: Singer) -> bool:
                # [V44] PRIMEIRO checa teve_vez. Se ja teve vez = JA PASSOU =
                # retorna False IMEDIATAMENTE, NÃO ADIANTA se ele e' ou nao e' o
                # now_singer_id. Nunca mais imunidade aqui.
                if self._singer_ja_teve_vez_na_rodada_atual(s):
                    return False
                return True

            # (1) GRUPO 1 PRIORIDADE MAX: Ainda nao teve vez NA RODADA.
            # =================================================================
            # [V38 CORRECAO FINAL - MINHA CULPA - rounds_sung MENOR PRIMEIRO!]
            # Antes V27-V37: ordenava SÓ por arrival_visual_unix (#1 #2 #3 #4)
            # → Bug: Junior rounds_sung=3, Jose rounds_sung=1 mas Junior vinha
            #   primeiro no começo de cada rodada (teve_vez resetado, SET vazio).
            # REGRA SAGRADA DO USUARIO: ninguém canta 2x antes de todo mundo
            # cantar 1x. Ninguém canta 3x antes de todo mundo cantar 2x.
            # SOLUCAO V38: chave dupla:
            #   CHAVE PRIMARIA: s.rounds_sung ASC (quem cantou MENOS → PRIMEIRO!)
            #   CHAVE SECUNDARIA: s.arrival_visual_unix ASC (desempatar se
            #                     rounds_sung IGUAL → ordem de chegada)
            # =================================================================
            grupo1_candidatos: list[Singer] = []
            for s in all_singers_visual_order:
                # [V44 BUG FINAL] So EXCLUI o now_singer_id do calculo de proximo
                # SE ELE AINDA NAO TEVE VEZ (ele esta la na tela CANTANDO AGORA e
                # obviamente nao pode ser "o proximo" ao mesmo tempo). Se JA TEVE
                # VEZ (ex: acabou musica, now_playing_user ainda nao zerou) → NAO
                # EXCLUI dele pq ele sera naturalmente excluido pelo helper
                # _ainda_nao_cantou (que retorna False p/ quem ja teve vez).
                if (not now_singer_ja_teve_vez) and now_singer_id and s.singer_id == now_singer_id:
                    continue
                if s.status == STATUS_LEFT_EARLY:
                    continue
                if _ainda_nao_cantou_rodada_v27(s):
                    grupo1_candidatos.append(s)
            if grupo1_candidatos:
                # [V41 CORRECAO FINAL - AGORA SOMENTE SEGUE A ORDEM JA DEFINIDA NO INICIO DA RODADA]
                # Não olha mais rounds_sung nem arrival; usa somente
                # ordem_chamada_na_rodada ASC, que foi gravada 1 ÚNICA VEZ no
                # começo da rodada (ou no deploy, caso meio da rodada). Isso
                # garante que, depois que Jose das Coves 1x foi chamado e
                # acabou a música, os restantes são SEMPRE Simone → Zequinha →
                # Junior, NUNCA MAIS muda a ordem no meio da rodada por causa
                # de aumento de rounds_sung.
                _rodada_atual_v41 = int(self._rodada_atual_geral or 1)
                def _v41_chave_rodada(s: Singer) -> tuple:
                    ord_val = int(getattr(s, "ordem_chamada_na_rodada", 0) or 0)
                    rod_val = int(getattr(s, "rodada_que_recebeu_ordem", 0) or 0)
                    if ord_val <= 0 or rod_val != _rodada_atual_v41:
                        # Fallback raro (cantor novo adicionado no MEIO da
                        # rodada, ninguém teve ordem ainda). Colocamos ele NO
                        # FIM da rodada atual (ordem 9999), e no INÍCIO da
                        # PRÓXIMA rodada ele já recebe ordem correta com
                        # rounds_sung.
                        return (99999, float(getattr(s, "arrival_visual_unix", 0.0) or 0.0))
                    return (ord_val, 0.0)
                grupo1_candidatos.sort(key=_v41_chave_rodada)
                next_singer_id_rodada = grupo1_candidatos[0].singer_id
            else:
                next_singer_id_rodada = None
            # (2) GRUPO 2 (Soh se GRUPO1 VAZIO = TODOS JA TIVERAM VEZ): RODADA CIRCULAR
            if not next_singer_id_rodada:
                now_visual_idx: int | None = None
                for i, s in enumerate(all_singers_visual_order):
                    if now_singer_id and s.singer_id == now_singer_id:
                        now_visual_idx = i
                        break
                found: str | None = None
                for i, s in enumerate(all_singers_visual_order):
                    if now_singer_id and s.singer_id == now_singer_id:
                        continue
                    if s.status in (STATUS_LEFT_EARLY, STATUS_SKIPPING):
                        continue
                    if now_visual_idx is not None and i <= now_visual_idx:
                        continue
                    if s.teve_vez_nesta_rodada or s.status in (STATUS_SUNG, STATUS_SKIPPING):
                        found = s.singer_id
                        break
                if not found:
                    for i, s in enumerate(all_singers_visual_order):
                        if now_singer_id and s.singer_id == now_singer_id:
                            continue
                        if s.status in (STATUS_LEFT_EARLY, STATUS_SKIPPING):
                            continue
                        if s.teve_vez_nesta_rodada or s.status in (STATUS_SUNG, STATUS_SKIPPING):
                            found = s.singer_id
                            break
                next_singer_id_rodada = found

            # Fallback: SKIPPING incluidos apenas se nao tiver mais ninguem
            if not next_singer_id_rodada:
                for s in all_singers_visual_order:
                    if now_singer_id and s.singer_id == now_singer_id:
                        continue
                    if s.status == STATUS_LEFT_EARLY:
                        continue
                    next_singer_id_rodada = s.singer_id
                    break
            if not next_singer_id_rodada:
                return (None, "")

            # ===== [HARD RULE #1 DUPLICATA V38: NAO VOLTA PARA GRUPO2 SE GRUPO1 TEM GENTE] =====
            grupo1_nao_atendido2_candidatos: list[Singer] = []
            for s in all_singers_visual_order:
                if now_singer_id and s.singer_id == now_singer_id:
                    continue
                if s.status == STATUS_LEFT_EARLY:
                    continue
                if _ainda_nao_cantou_rodada_v27(s):
                    grupo1_nao_atendido2_candidatos.append(s)
            if grupo1_nao_atendido2_candidatos:
                # [V41 CORRECAO FINAL - HARD RULE 1 tb segue ordem fixa da rodada]
                # Mesma chave V41: ordem_chamada_na_rodada ASC, só se rodada válida.
                _rodada_hard = int(self._rodada_atual_geral or 1)
                def _chave_g1_hard(s: Singer) -> tuple:
                    ord_val = int(getattr(s, "ordem_chamada_na_rodada", 0) or 0)
                    rod_val = int(getattr(s, "rodada_que_recebeu_ordem", 0) or 0)
                    if ord_val <= 0 or rod_val != _rodada_hard:
                        return (99999, float(getattr(s, "arrival_visual_unix", 0.0) or 0.0))
                    return (ord_val, 0.0)
                grupo1_nao_atendido2_candidatos.sort(key=_chave_g1_hard)
                grupo1_nao_atendido2 = grupo1_nao_atendido2_candidatos[0].singer_id
            else:
                grupo1_nao_atendido2 = None
            if grupo1_nao_atendido2 is not None and (next_singer_id_rodada != grupo1_nao_atendido2):
                logging.error(
                    "[HARD RULE #1 V41 get_next()] ia pular GRUPO1! cand=%r -> forca GRUPO1 first=%r. now=%r todos=%s",
                    next_singer_id_rodada, grupo1_nao_atendido2,
                    now_singer_id,
                    [(s.name, s.status, s.rounds_sung, s.teve_vez_nesta_rodada,
                      f"ord_rod={int(getattr(s,'ordem_chamada_na_rodada',0))}")
                     for s in all_singers_visual_order],
                )
                next_singer_id_rodada = grupo1_nao_atendido2

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

                # ========== [V34 SYNC STATUS ABSOLUTO: NAO CONFIA NO STATUS SALVO!] ==========
                # [BUG 13/08 14:05] Zequinha aparecia "✅ Tem musica" (status WAITING)
                # mas NA FILA REAL NAO TINHA NENHUMA MUSICA DELE. O problema era que o
                # status do JSON salvo (WAITING) nao era atualizado quando a fila dele
                # esvaziava (musica tocou e acabou). Sync a CADA refresh com a
                # FILA REAL DO MOMENTO, 100% das vezes, e nao confia no status salvo.
                #
                # REGRAS V34 DE STATUS (APLICAM SEMPRE, EXCETO LEFT_EARLY / SKIPPING):
                #   [TEM MUSICA NA FILA REAL AGORA] --> STATUS = WAITING   (mesmo que estivesse SUNG)
                #   [NAO TEM NENHUMA MUSICA NA FILA REAL AGORA] --> STATUS = PENDING_MUSIC  (mesmo que estivesse SUNG / WAITING)
                #
                # OBS 1: LEFT_EARLY nunca mexe. Operador que definiu.
                # OBS 2: SKIPPING: se tem musica → volta WAITING; se sem musica → PENDING;
                #   pulo de rodada já marcaria teve_vez = True entao ele nao volta no GRUPO1 nesta rodada.
                # OBS 3: SUNG (status antigo) → sempre atualizamos para WAITING ou
                #   PEND dependendo de musica na fila. (Teve_vez = True continua, nao apaga).
                # OBS 4: CANTANDO AGORA (now_singer_id) → não muda status.
                # OBS 5 (V44 BUG SIMONE tarja vermelha piscando): Respeita clique
                #   MANUAL do operador nos botoes ✅ Tem musica / 🔔 Sem musica do card
                #   por 2 minutos (120s). Se status_manualmente_marcado_at_unix for
                #   recente (<120s), NAO SOBRESCREVE o Sync V34. O operador que mandou.
                e_cantando_agora = bool(now_singer_id and str(now_singer_id) == str(s.singer_id))
                # Aplica regras V34 absoluto (exceto se for LEFT_EARLY)
                if s.status != STATUS_LEFT_EARLY and not e_cantando_agora:
                    # SKIPPING: apenas para calcular "teve vez". Em termos de status
                    # musica, ele vira normal (nao fica com skip sem motivo).
                    alvo = ""
                    if has_music:
                        alvo = STATUS_WAITING
                    else:
                        alvo = STATUS_PENDING_MUSIC
                    # [V44 PROTECAO CLIQUE MANUAL 120s]
                    # Se operador clicou nos botoes do card a MENOS DE 120s, o status
                    # dele "vale" por esse tempo, mesmo que contradiga a fila real.
                    # Resolve bug do Simone: ela clicou Tem musica mas Sync V34 voltava
                    # Sem musica em 1s, e tarja vermelha piscava sem parar.
                    marcou_manual_recente = False
                    try:
                        tstamp_manual = float(getattr(s, "status_manualmente_marcado_at_unix", 0.0) or 0.0)
                        if tstamp_manual > 0 and (time.time() - tstamp_manual) < 120.0:
                            marcou_manual_recente = True
                    except Exception:
                        pass
                    if marcou_manual_recente:
                        # Nao sobrescreve status do operador, mantem. Mas atualiza updated_at para nao ficar stale.
                        s.updated_at_unix = time.time()
                        changed += 1
                    elif str(s.status) != str(alvo):
                        oldstat = s.status
                        s.status = alvo
                        s.updated_at_unix = time.time()
                        changed += 1
                        try:
                            logging.info(
                                "[V34 SYNC STATUS %s → %s] (tem_musica=%r, left_early=%r, cantando_agora=%r, tev_vez_nesta_rod=%r, rounds_sung=%r)",
                                str(oldstat), str(alvo), bool(has_music),
                                bool(s.status == STATUS_LEFT_EARLY), bool(e_cantando_agora),
                                bool(getattr(s, "teve_vez_nesta_rodada", False)),
                                int(getattr(s, "rounds_sung", 0) or 0),
                            )
                        except Exception:
                            pass
                # ==================================================================
            if changed:
                self._save()

        # =============== REGRA SAGRADA 2026-08-13 (V27) ===============
        # [MUDANÇA MAIS IMPORTANTE V27]:
        # ANTES: GRUPO1 era definido por "status NOT IN (SUNG, SKIPPING, LEFT_EARLY)".
        # ISSO FALHAVA quando o operador clicava em "🔔 Sem música" no cantor que
        # JÁ CANTARA (SUNG) → status virava PENDING_MUSIC de novo, ele entrava no
        # GRUPO1 e voltava para a FRENTE da fila, passando na frente de quem
        # AINDA NÃO CANTOU NENHUMA VEZ NA RODADA (ex: Simone #2).
        #
        # AGORA V27: a VERDADE ÚNICA de "já foi atendido nesta rodada" é o campo
        # Singer.teve_vez_nesta_rodada (BOLEAN + rodada_atual_sistema).
        #  - Este campo é SÓ SETADO PARA TRUE quando o cantor: (a) ACABA A MUSICA
        #    DE VERDADE (SUNG no fim de playback), (b) CLIQUE em "Pular rodada"
        #    (SKIPPING), (c) "Saiu cedo" (LEFT_EARLY).
        #  - Clique em "🔔 Sem música" NÃO ZERA este campo.
        #  - Clique em "✅ Tem música" NÃO ZERA este campo.
        #
        # O cantor SÓ VOLTA para GRUPO1 novamente QUANDO TODOS os cantores ativos
        # já tiveram teve_vez_nesta_rodada=True → neste momento o sistema
        # INCREMENTA _rodada_atual_geral (1→2, 2→3 etc) e zera
        # teve_vez_nesta_rodada=False para TODOS automaticamente.
        #
        # Com isto: ordem 1(JUNIOR) → 2(SIMONE) → 3(JOSE) → 4(ZEQUNA) é
        # MATEMATICAMENTE GARANTIDA em CADA RODADA, NUNCA MAIS pula ninguém.
        next_singer_id_rodada = None
        all_singers_visual_order: list[Singer] = []
        with self._lock:
            all_singers_visual_order = list(self._singers)
        all_singers_visual_order.sort(key=lambda s: s.arrival_visual_unix)

        # [V33 CORRECAO BUG RODADA 2 NAO INICIA 13/08/26 13:45]
        # PROBLEMA: quando o último cantor (ex: Zequinha #4) acabava de cantar,
        # refresh_statuses_from_queue rodava, calculava GRUPO1 (ainda não cantou)
        # VAZIO → ia para GRUPO2 (SUNG/SKIPPING) que também NÃO retorna um "next
        # válido" (pois no mesmo instante em que status vira SUNG, o SET
        # infalível/teve_vez/rounds_sung foram definidos mas ninguém ainda tinha
        # disparado nova_rodada). Resultado: next_singer_id_rodada = None → tela
        # fica sem ninguém como próximo (estado morto).
        #
        # SOLUCAO V33: ANTES DE TUDO, TENTAR INICIAR NOVA RODADA SE JA FOR
        # MOMENTO (todos ativos com teve_vez=True). Depois só então calcular
        # GRUPO1/GRUPO2 com os campos novos já zerados.
        #
        # Passo 0 (V33): verificar nova rodada ANTES de calcular next.
        self._verificar_inicio_nova_rodada_e_reinicializar()

        # [V41 CORRECAO FINAL - ORDEM FIXA POR RODADA - CHAMADA OBRIGATORIA ANTES DE TODO CALCULO DE NEXT]
        # Garante que ordem da rodada atual já foi calculada UMA ÚNICA VEZ e está
        # fixa nos campos Singer.ordem_chamada_na_rodada. Mesmo que estejamos no
        # MEIO da rodada (deploy/restart JSON antigo V38), define ordem por chegada.
        try:
            self._garantir_ordem_chamada_rodada_atual_definida_se_necessario(all_singers_visual_order)
        except Exception as e:
            logging.exception("[V41 ERRO NO REFRESH ao definir ordem chamada rodada!] %s", str(e))

        _rodada_atual_v41_refresh = int(self._rodada_atual_geral or 1)
        def _v41_chave_rodada_refresh(s: Singer) -> tuple:
            ord_val = int(getattr(s, "ordem_chamada_na_rodada", 0) or 0)
            rod_val = int(getattr(s, "rodada_que_recebeu_ordem", 0) or 0)
            if ord_val <= 0 or rod_val != _rodada_atual_v41_refresh:
                return (99999, float(getattr(s, "arrival_visual_unix", 0.0) or 0.0))
            return (ord_val, 0.0)

        # [V44 BUG FINAL 16:21 - JUNIOR 2X SEGUIDAS (mesmo no refresh!)]
        # Mesma correcao aplicada no get_next(). O now_singer_id (tocando agora)
        # so tem imunidade de exclusao do calculo SE ELE AINDA NAO TEVE VEZ.
        # Se ja teve vez (ex: musica acabou de acabar e o now_playing_user ainda
        # nao foi zerado pelo playback por race), o now sai dos calculos por
        # teve_vez=True normalmente, nao pelo skip.
        now_singer_ja_teve_vez_refresh = False
        if now_singer_id:
            for s in all_singers_visual_order:
                if str(s.singer_id) == str(now_singer_id):
                    if self._singer_ja_teve_vez_na_rodada_atual(s):
                        now_singer_ja_teve_vez_refresh = True
                    break

        # Helper local: cantor esta em "GRUPO Ainda Nao Cantou"?
        def _ainda_nao_cantou_rodada(s: Singer) -> bool:
            # [V44] PRIMEIRO: se ja teve vez na rodada = JA PASSOU, retorna False JA.
            # Nao existe mais imunidade de "sou now_singer_id e mesmo assim passo".
            if self._singer_ja_teve_vez_na_rodada_atual(s):
                return False
            return True

        # ---------- GRUPO 1 (prioridade maxima): Ainda nao cantou nesta rodada ----------
        # =================================================================
        # [V41 CORRECAO FINAL - ORDEM FIXA DA RODADA - NUNCA MAIS MUDA NO MEIO DA RODADA]
        # ANTES (V38): reordenava a cada refresh por (rounds_sung ASC, arrival ASC)
        # → BUG: quando um cantor terminava a musica, rounds_sung aumentava, V38
        # recalculava toda a ordem do resto da fila NO MEIO DA RODADA, podendo
        # pular cantores ou inverter sequencia. Usuario reclamou de "logica da fila
        # e seus disparos".
        # AGORA (V41): usa SOMENTE Singer.ordem_chamada_na_rodada ASC, que foi
        # calculado UMA UNICA VEZ no INICIO da rodada e NUNCA MAIS muda durante
        # toda a rodada. Sequencia 1-2-3-4 é MATEMATICAMENTE GARANTIDA até fim da rodada.
        # =================================================================
        grupo1_candidatos_refresh: list[Singer] = []
        for s in all_singers_visual_order:
            # [V44] So skip do now se ele ainda nao teve vez (esta tocando agora de verdade)
            if (not now_singer_ja_teve_vez_refresh) and now_singer_id and s.singer_id == now_singer_id:
                continue
            # LEFT_EARLY já sai automaticamente (cai em _singer_ja_teve_vez = True)
            if not _ainda_nao_cantou_rodada(s):
                continue
            grupo1_candidatos_refresh.append(s)
        if grupo1_candidatos_refresh:
            grupo1_candidatos_refresh.sort(key=_v41_chave_rodada_refresh)
            next_singer_id_rodada = grupo1_candidatos_refresh[0].singer_id
        else:
            next_singer_id_rodada = None

        # ---------- GRUPO 2 (RODADA CIRCULAR, SOH SE GRUPO1 VAZIO) ----------
        # Significa: TODOS ativos já tiveram teve_vez_nesta_rodada=True
        # (ou seja: NOVA RODADA VAI COMEÇAR. Antes de começar, podemos retornar
        # o primeiro da nova rodada, ou aguardar a função nova rodada.)
        if not next_singer_id_rodada:
            now_visual_idx = None
            for i, s in enumerate(all_singers_visual_order):
                if now_singer_id and s.singer_id == now_singer_id:
                    now_visual_idx = i
                    break
            # Primeiro SUNG / teve vez DEPOIS do now
            found = None
            for i, s in enumerate(all_singers_visual_order):
                # [V44] So skip now se ele ainda NAO teve vez (esta tocando agora de verdade)
                if (not now_singer_ja_teve_vez_refresh) and now_singer_id and s.singer_id == now_singer_id:
                    continue
                if s.status == STATUS_LEFT_EARLY:
                    continue
                if now_visual_idx is not None and i <= now_visual_idx:
                    continue
                if s.teve_vez_nesta_rodada or s.status == STATUS_SUNG or s.status == STATUS_SKIPPING:
                    found = s.singer_id
                    break
            if not found:
                for i, s in enumerate(all_singers_visual_order):
                    # [V44] So skip now se ele ainda NAO teve vez
                    if (not now_singer_ja_teve_vez_refresh) and now_singer_id and s.singer_id == now_singer_id:
                        continue
                    if s.status == STATUS_LEFT_EARLY:
                        continue
                    if s.teve_vez_nesta_rodada or s.status == STATUS_SUNG or s.status == STATUS_SKIPPING:
                        found = s.singer_id
                        break
            next_singer_id_rodada = found

        # ---------- Fallback geral ----------
        if not next_singer_id_rodada:
            for s in all_singers_visual_order:
                # [V44] So skip now se ele AINDA NAO teve vez (cantando agora realmente)
                if (not now_singer_ja_teve_vez_refresh) and now_singer_id and s.singer_id == now_singer_id:
                    continue
                if s.status == STATUS_LEFT_EARLY:
                    continue
                next_singer_id_rodada = s.singer_id
                break

        # ---------- [HARD RULE #1 - DUPLA CAMADA V41] ----------
        # Mesmo que tenha bug em quaisquer dos blocos acima, REFAZ o loop e
        # GARANTE: se ALGUEM do GRUPO1 (ainda não teve vez, não skipping/left)
        # existe → sobreescreve TUDO e coloca ele como next.
        # [V41]: MESMA ordenação POR ORDEM FIXA DA RODADA (ordem_chamada_na_rodada ASC),
        # NUNCA MAIS reordena por rounds_sung dinamico no meio da rodada!
        grupo1_nao_atendido_cand: list[Singer] = []
        for s in all_singers_visual_order:
            # [V44] So skip now se ele ainda NAO teve vez
            if (not now_singer_ja_teve_vez_refresh) and now_singer_id and s.singer_id == now_singer_id:
                continue
            if s.status == STATUS_LEFT_EARLY:
                continue
            if _ainda_nao_cantou_rodada(s):
                grupo1_nao_atendido_cand.append(s)
        if grupo1_nao_atendido_cand:
            grupo1_nao_atendido_cand.sort(key=_v41_chave_rodada_refresh)
            grupo1_nao_atendido = grupo1_nao_atendido_cand[0].singer_id
        else:
            grupo1_nao_atendido = None
        if grupo1_nao_atendido is not None and (next_singer_id_rodada != grupo1_nao_atendido):
            logging.error(
                "[HARD RULE #1 V41 CORRECAO AUTOMATICA] next ia pular GRUPO1! "
                "next_candidato=%r -> sobreescrito para GRUPO1 first=%r. "
                "now_singer_id=%r todos=(name, status, ord_rod, rounds_sung, teve_vez, rod_teve_vez)=%s",
                next_singer_id_rodada, grupo1_nao_atendido,
                now_singer_id,
                [(s.name, s.status,
                  int(getattr(s, "ordem_chamada_na_rodada", 0) or 0),
                  s.rounds_sung, s.teve_vez_nesta_rodada, s.rodada_que_teve_vez)
                 for s in all_singers_visual_order],
            )
            next_singer_id_rodada = grupo1_nao_atendido

        # ---------- [V27 PASSO CRITICO: VERIFICAR SE TODOS JA FORAM → NOVA RODADA] ----------
        # A função abaixo, se TODOS tiveram vez, incrementa _rodada_atual_geral
        # e zera teve_vez_nesta_rodada=False em TODOS. Rodada 2/3/4 começa.
        rodada_mudou = self._verificar_inicio_nova_rodada_e_reinicializar()
        if rodada_mudou:
            # [V41 CORRECAO FINAL - NOVA RODADA DENTRO DO REFRESH]:
            # _verificar_inicio_nova_rodada_e_reinicializar() ACABOU DE ZERAR os campos
            # ordem_chamada_na_rodada e rodada_que_recebeu_ordem de TODOS (porque
            # rodada nova). Precisamos DEFINIR A ORDEM DA NOVA RODADA AGORA, UMA
            # UNICA VEZ, antes de ordenar o grupo.
            try:
                # Atualiza referencia da rodada atual para o sort
                _rodada_atual_v41_refresh = int(self._rodada_atual_geral or 1)
                self._garantir_ordem_chamada_rodada_atual_definida_se_necessario(all_singers_visual_order)
            except Exception as e:
                logging.exception("[V41 ERRO NO REFRESH ao definir ordem NOVA RODADA!] %s", str(e))
            # Nova rodada começou! Recalcula next_singer_id_rodada = PRIMEIRO do GRUPO1 de novo (agora todos são GRUPO1)
            # [V41]: usa ORDEM FIXA DA NOVA RODADA (ordem_chamada_na_rodada ASC) calculada acima,
            # NÃO MAIS rounds_sung dinamico. Sequencia fixa para TODA a nova rodada.
            grupo1_nova_rodada: list[Singer] = []
            for s in all_singers_visual_order:
                # [V44] So skip now se ele ainda NAO teve vez
                if (not now_singer_ja_teve_vez_refresh) and now_singer_id and s.singer_id == now_singer_id:
                    continue
                if s.status == STATUS_LEFT_EARLY:
                    continue
                if not self._singer_ja_teve_vez_na_rodada_atual(s):
                    grupo1_nova_rodada.append(s)
            next_singer_id_rodada = None
            if grupo1_nova_rodada:
                grupo1_nova_rodada.sort(key=_v41_chave_rodada_refresh)
                next_singer_id_rodada = grupo1_nova_rodada[0].singer_id
            logging.warning(
                "[NOVA RODADA DETECTADA V41 ORDEM FIXA] nova rodada=%s, proximo=%s, "
                "todos=(name, ord_rod_nova, rounds_sung, teve_vez)=%s",
                self._rodada_atual_geral, next_singer_id_rodada,
                [(s.name,
                  int(getattr(s, "ordem_chamada_na_rodada", 0) or 0),
                  s.rounds_sung, s.teve_vez_nesta_rodada) for s in all_singers_visual_order],
            )

        # ---------- Ultimo fallback (SOMENTE SE GRUPO1 VAZIO): next_user da fila real ----------
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
            "rodada_atual": int(self._rodada_atual_geral),
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
            # [LEI VEZ SAGRADA V31 - CARREGAR rodada_atual_geral DO JSON SALVO]
            # Antes V27-V30: sempre iniciava rodada=1 no __init__, reiniciava tudo em
            # todo restart do service. Agora: se tiver salvo no JSON, usa esse valor
            # para continuar a contagem corretamente (evita erros de fallback
            # rounds_sung >= rodada_atual com 2+ dias de atividade contínua).
            try:
                rodada_salva = int(raw.get("rodada_atual_geral") or 0)
                if rodada_salva > 0:
                    self._rodada_atual_geral = int(rodada_salva)
            except Exception:
                pass
            # [LEI VEZ SAGRADA V31 - CARREGAR SET INFALÍVEL DE PASSARAM NESTA RODADA]
            # Se tem ids salvos no JSON, popular o set em memória. Mesmo que todos
            # os outros fallbacks falhem, camada 0 (SET) retorna que eles já passaram.
            try:
                ids_salvos = raw.get("singers_passaram_nesta_rodada_ids") or []
                if isinstance(ids_salvos, (list, tuple, set)):
                    self._singers_passaram_nesta_rodada_ids = {str(x) for x in ids_salvos if str(x).strip()}
            except Exception:
                pass
            singers = []
            qualquer_mudanca = False
            _rodada_geral = int(self._rodada_atual_geral) if hasattr(self, "_rodada_atual_geral") and self._rodada_atual_geral else 1
            for item in raw.get("singers", []) or []:
                try:
                    if "arrival_visual_unix" not in item or not float(item.get("arrival_visual_unix") or 0.0):
                        item["arrival_visual_unix"] = float(item.get("arrival_unix") or 0.0)
                        qualquer_mudanca = True
                    # [LEI DA VEZ SAGRADA V28 - BACKWARD COMPATIBILITY - BUG RAIZ DESCOBERTO 13/08/2026]
                    # Problema: o JSON salvo ANTES do deploy V27 nao tem os campos novos
                    # teve_vez_nesta_rodada / rodada_que_teve_vez / rodada_atual_sistema.
                    # Os valores default eram False / 0 / 1 = entao Junior que JA ESTAVA
                    # SUNG (ja cantou ontem / semana passada) recebia teve_vez=False e
                    # VOLTAVA PARA O INICIO DA FILA (GRUPO 1) → bug do usuário: Junior
                    # acabou de cantar, proximo volta a ser Junior de novo e Simone nunca tem vez!
                    #
                    # Solucao: SE o status do cantor JA FORA SUNG / SKIPPING / LEFT_EARLY
                    # (ele ja teve sua vez) E NAO TEM os campos novos no JSON (campos ausentes)
                    # → MARCA teve_vez_nesta_rodada = True + valores defaults sensatos.
                    # Garante que quem ja cantou nao volta pro começo DEPOIS do deploy!
                    _tinha_campo_tevevez = "teve_vez_nesta_rodada" in item and bool(item.get("teve_vez_nesta_rodada", False))
                    _tinha_rodada_sistema = "rodada_atual_sistema" in item and int(item.get("rodada_atual_sistema", 0) or 0) > 0
                    _rounds_sung_item = int(item.get("rounds_sung", 0) or 0)
                    _rodada_geral = int(self._rodada_atual_geral) if hasattr(self, "_rodada_atual_geral") and self._rodada_atual_geral else 1
                    if (not _tinha_campo_tevevez) and (not _tinha_rodada_sistema):
                        _status = str(item.get("status", "") or "")
                        _deve_marcar = False
                        if _status in (STATUS_SUNG, STATUS_SKIPPING, STATUS_LEFT_EARLY):
                            _deve_marcar = True
                        # [LEI VEZ SAGRADA V29 - FALLBACK rounds_sung no _load]
                        # MESMO SE _status nao for SUNG (ele voltou PENDING por clique/Sem musica
                        # antes do restart do service), se rounds_sung >= rodada_atual → ele
                        # ja teve vez! Fallback rounds_sung tambem aqui no load para nao
                        # depender de status atual!
                        if (not _deve_marcar) and (_rounds_sung_item >= _rodada_geral):
                            _deve_marcar = True
                        if _deve_marcar:
                            item["teve_vez_nesta_rodada"] = True
                            item["rodada_que_teve_vez"] = _rodada_geral
                            item["rodada_atual_sistema"] = _rodada_geral
                            qualquer_mudanca = True
                    singer = Singer(**{k: item[k] for k in item if k in Singer.__dataclass_fields__})
                    # Dupla checagem POS-construtor (para casos onde _status ficou gravado errado):
                    if (not singer.teve_vez_nesta_rodada):
                        _deve_pos = False
                        if singer.status in (STATUS_SUNG, STATUS_SKIPPING, STATUS_LEFT_EARLY):
                            _deve_pos = True
                        # Fallback rounds_sung POS-construtor tambem:
                        if (not _deve_pos) and int(getattr(singer, "rounds_sung", 0) or 0) >= _rodada_geral:
                            _deve_pos = True
                        if _deve_pos:
                            singer.teve_vez_nesta_rodada = True
                            singer.rodada_que_teve_vez = max(1, _rodada_geral)
                            singer.rodada_atual_sistema = max(1, _rodada_geral)
                            qualquer_mudanca = True
                    singers.append(singer)
                except Exception:
                    continue
            singers.sort(key=lambda s: s.arrival_visual_unix)
            self._singers = singers
            # Se mudamos QUALQUER coisa (adicionamos campos novos em cantores antigos)
            # → SALVA de volta no JSON AGORA, para na proxima inicializacao nao precisar
            # de fallback nenhum (e para operador ver no JSON que foi salvo corretamente).
            if qualquer_mudanca:
                try:
                    data = {"saved_at": time.time(), "singers": [asdict(s) for s in self._singers]}
                    self._persist_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
                except Exception:
                    pass

    def _save(self) -> None:
        with self._lock:
            # [LEI VEZ SAGRADA V31 - SALVAR TUDO QUE É GLOBAL DO MANAGER]
            # Salvar rodada_atual_geral para não voltar para 1 em todo restart do
            # service. Antes V27-V30, restart service sempre voltava rodada para
            # 1 quebrava fallback rounds_sung >= rodada_atual em casos de 2+ dias.
            # Também salvar o SET INFALÍVEL de quem já passou nesta rodada, para
            # mesmo que reinicie tudo no meio de uma rodada, a lista de quem já
            # passou permaneça íntegra.
            try:
                ids_set_list = sorted(str(x) for x in list(self._singers_passaram_nesta_rodada_ids or set()))
            except Exception:
                ids_set_list = []
            data = {
                "saved_at": time.time(),
                "rodada_atual_geral": int(self._rodada_atual_geral or 1),
                "singers_passaram_nesta_rodada_ids": ids_set_list,
                "singers": [asdict(s) for s in self._singers],
            }
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

        [LEI DA VEZ SAGRADA V27 - AQUI É MARCADO "ELE JÁ TEVE VEZ"]:
          - Quando status vira SUNG (já cantou) / SKIPPING (pular rodada) / LEFT_EARLY (saiu cedo)
            → MARCA teve_vez_nesta_rodada = True. ESTA MARCA NÃO É APAGADA ATÉ A
              PRÓXIMA RODADA (quando TODOS tiveram vez). NÃO É APAGADA nem por "Sem música",
              nem por "Tem música". Resolve o bug do Junior que já cantou voltar na frente
              da Simone porque alguém clicou Sem música.
          - Quando status vira PENDING_MUSIC / WAITING (Sem música / Tem música):
            → NÃO TOCA NA MARCA teve_vez_nesta_rodada (mantém como estava).
        """
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
            # [BUG V43 TARJA - RESPEITA CLIQUE MANUAL DO OPERADOR por 2 min]
            # Quando o operador clica em "✅ Tem musica" (WAITING) ou "🔔 Sem musica" (PENDING)
            # nos botoes do card → grava a hora em status_manualmente_marcado_at_unix.
            # O Sync V34 nao vai sobrescrever por 120s. Resolve bug Simone tarja vermelha
            # piscando mesmo quando o usuario clicou em Tem musica.
            if new_status in (STATUS_WAITING, STATUS_PENDING_MUSIC):
                singer.status_manualmente_marcado_at_unix = float(time.time())
            # ========== [LEI VEZ SAGRADA V27 - MARCA TEVE VEZ AQUI] ==========
            if new_status in (STATUS_SUNG, STATUS_SKIPPING, STATUS_LEFT_EARLY):
                # Só marca se AINDA NÃO TINHA MARCADO (evita sobreescrever rodada_que_teve_vez)
                if not singer.teve_vez_nesta_rodada or singer.rodada_atual_sistema != self._rodada_atual_geral:
                    self._marcar_teve_vez_na_rodada(singer)
            # (caso contrario — PENDING_MUSIC / WAITING — NÃO ZERA marca, intencionalmente!)
            # ==================================================================
            if new_status == STATUS_SKIPPING and old_status != STATUS_SKIPPING:
                singer.arrival_unix = time.time() + 0.001
            if new_status == STATUS_SUNG and old_status != STATUS_SUNG:
                singer.rounds_sung += 1
                singer.arrival_unix = time.time() + 0.001
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
                # Tambem marca como marcado manualmente/por adicao real (respeita por 2 min Sync V34):
                singer.status_manualmente_marcado_at_unix = float(time.time())
                self._save()
            return singer

    def remove_singer(self, singer_id: str) -> bool:
        """Remove cantor da lista.

        [V46 COMPORTAMENTO EXPLICITO DO USUARIO - 13/08/2026 - REDUNDANCIA COM SAIU CEDO]
        Ao deletar usuario: considera ele como "Saiu cedo" desta rodada, para
        nao quebrar a contagem de cantores que ja passaram e nem gerar loop
        infinito esperando um cantor que nao existe mais. Depois, REMONTA a
        `ordem_chamada_na_rodada` dos cantores RESTANTES, MANTER A ORDEM DE
        CHEGADA original (ordem ja estabelecida), apenas reajustando os numeros
        (ex: rodada era 1,2,3,4 -> apagar 2 -> vira 1,2,3 para os 3 restantes).
        Nao recalcula do zero, nao reordena por rounds_sung, nao mexe em nada,
        so ajusta o numero da ordem.
        """
        with self._lock:
            # 1) Marca o removido como se fosse "saiu cedo / ja passou nesta rodada"
            #    (camadas 1,2,3 de protecao da lei da vez sagrada)
            removido = next((s for s in self._singers if s.singer_id == singer_id), None)
            if removido is not None:
                try:
                    removido.teve_vez_nesta_rodada = True
                    try:
                        removido.rodada_que_teve_vez = int(self._rodada_atual_geral or 1)
                    except Exception:
                        pass
                    try:
                        if hasattr(self, "_singers_passaram_nesta_rodada_ids"):
                            if self._singers_passaram_nesta_rodada_ids is None:
                                self._singers_passaram_nesta_rodada_ids = set()
                            self._singers_passaram_nesta_rodada_ids.add(str(removido.singer_id))
                    except Exception:
                        pass
                    logging.warning(
                        "[V46 REMOVE_SINGER = SAIU CEDO] Cantor deletado=%r "
                        "id=%r. Marcado como teve_vez_nesta_rodada=True (camadas "
                        "1,2,3) para nao quebrar a rodada atual. Restantes=%d.",
                        getattr(removido, "name", ""), removido.singer_id,
                        len(self._singers) - 1
                    )
                except Exception as exc_rm:
                    logging.warning("[V46 REMOVE_SINGER mark saiu_cedo falhou (ignorado): %s", exc_rm)

            # 2) Remove o cantor da lista
            n = len(self._singers)
            self._singers = [s for s in self._singers if s.singer_id != singer_id]
            removed_something = (len(self._singers) != n)

            # 3) Se removeu, REMONTA A ORDEM DA RODADA DOS RESTANTES
            #    (MANTER ORDEM DE CHEGADA original; so reajusta os numeros)
            if removed_something:
                try:
                    _rod_atual = int(getattr(self, "_rodada_atual_geral", 1) or 1)
                    # Filtra apenas os ativos (nao left_early) e que pertencem a esta rodada
                    restantes: list[Singer] = []
                    for s in self._singers:
                        try:
                            st = str(getattr(s, "status", "") or "")
                        except Exception:
                            st = ""
                        if st == "left_early":
                            continue
                        restantes.append(s)

                    # Ordena EXCLUSIVAMENTE por ordem de chegada (a ordem ja estabelecida)
                    restantes.sort(
                        key=lambda s: float(getattr(s, "arrival_visual_unix", 0.0) or 0.0)
                    )

                    # Reatribui `ordem_chamada_na_rodada` = 1..N
                    for idx, s in enumerate(restantes):
                        try:
                            s.ordem_chamada_na_rodada = int(idx + 1)
                            s.rodada_que_recebeu_ordem = int(_rod_atual)
                        except Exception:
                            pass

                    if len(restantes) >= 1:
                        _seq = ", ".join(
                            [f"#{int(getattr(s,'ordem_chamada_na_rodada',0))} "
                             f"{getattr(s,'name','?')}" for s in restantes]
                        )
                        logging.warning(
                            "[V46 REMOVE_SINGER ORDEM REMONTADA (chegada original, sem reordenar)] "
                            "Rodada #%d. Nova sequencia da rodada AJustada: %s",
                            _rod_atual, _seq
                        )
                except Exception as exc_reord:
                    logging.warning("[V46 REMOVE_SINGER remontar ordem falhou (ignorado): %s", exc_reord)

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
