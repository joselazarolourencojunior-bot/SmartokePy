"""Queue management for PiKaraoke.

Handles song queue operations including enqueueing, editing, clearing,
and fair queue algorithm.
"""

from __future__ import annotations

import logging
import random
from typing import Any, Callable

from flask_babel import _

from pikaraoke.lib.events import EventSystem
from pikaraoke.lib.preference_manager import PreferenceManager


class QueueManager:
    """Manages the song queue: enqueueing, editing, reordering, and fair queue logic."""

    def __init__(
        self,
        preferences: PreferenceManager,
        events: EventSystem,
        get_now_playing_user: Callable[[], str | None] | None = None,
        filename_from_path: Callable[[str, bool], str] | None = None,
        get_available_songs: Callable[[], Any] | None = None,
    ) -> None:
        self.queue: list[dict[str, Any]] = []
        self._preferences = preferences
        self._events = events
        self._get_now_playing_user = get_now_playing_user
        self._filename_from_path = filename_from_path
        self._get_available_songs = get_available_songs
        self._last_popped_user_key: str | None = None
        self._round_seen_user_keys: set[str] = set()

    def _user_key(self, user: str) -> str:
        return " ".join(user.split()).casefold()

    @staticmethod
    def _parse_mesa_and_singer(user: str) -> tuple[str, str]:
        """Extrai (mesa_codigo, nome_cantor) a partir do user string.

        Padroes aceitos (case insensitive):
          - "mesa-1: Carlos"             -> ("mesa-1", "Carlos")
          - "Mesa 10 - Ana Clara"        -> ("mesa-10", "Ana Clara")
          - "mesa_05|Pedro"              -> ("mesa-5", "Pedro")
          - "[mesa-28]  Maria Silva"     -> ("mesa-28", "Maria Silva")
          - "Carlos" (sem mesa)          -> ("global", "Carlos")
          - "Operador" / "Admin" / vazio -> ("global", ...mantido)

        Normaliza codigo da mesa para "mesa-<N>" (N inteiro sem 0-padding).
        """
        s = " ".join(str(user or "").split())
        if not s.strip():
            return ("global", "")

        mesa = "global"
        singer = s.strip()

        # 1) Tenta extrair "mesa<sep><digitos>" no INICIO ou em qualquer lugar
        patterns = [
            r"^\s*\[?\s*mesa\s*[\-_ :/|]?\s*0*(\d+)\s*\]?\s*[\-:|\s]*\s*(.*)$",
            r"\bmesa\s*[\-_ :/|]?\s*0*(\d+)\s*",
        ]
        import re as _re

        m1 = _re.match(patterns[0], s, _re.IGNORECASE)
        if m1:
            n = int(m1.group(1))
            resto = (m1.group(2) or "").strip(" :|-_|[]\t")
            mesa = f"mesa-{n}"
            if resto:
                singer = resto
        else:
            m2 = _re.search(patterns[1], s, _re.IGNORECASE)
            if m2:
                n = int(m2.group(1))
                mesa = f"mesa-{n}"
                before = s[: m2.start()].strip(" :|-_|[]\t")
                after = s[m2.end():].strip(" :|-_|[]\t")
                singer = " ".join([x for x in [before, after] if x]).strip() or s.strip()

        singer_clean = singer.strip(" :|-_|[]\t")
        singer_out = singer_clean or s.strip()
        return (mesa.casefold(), singer_out.casefold())

    def is_singer_name_already_in_same_mesa(self, user: str) -> tuple[bool, str, str]:
        """Verifica se ja existe outro cantor com MESMO NOME na MESMA MESA (na fila ou tocando agora).

        Returns: (conflict: bool, conflicting_user_display: str, conflicting_mesa: str)
        """
        mesa_nova, singer_nova = self._parse_mesa_and_singer(user)
        if not singer_nova or singer_nova in {"", "global", "pikaraoke", "randomizer", "admin", "operador"}:
            return (False, "", mesa_nova)
        now_playing_user = self._get_now_playing_user() if self._get_now_playing_user else None
        all_users = [now_playing_user] if now_playing_user else []
        all_users += [item.get("user") for item in self.queue if item.get("user")]

        for other_user in all_users:
            if other_user is None:
                continue
            m_out, s_out = self._parse_mesa_and_singer(other_user)
            if not s_out:
                continue
            # Bloqueia SEMPRE que (mesa normalizada + nome normalizado) coincida
            # Nao importa se eh a mesma string user ou outra pessoa com mesmo nome na mesa
            # ou mesmo user repetindo (deve escolher outro nome, p ex "Carlos Silva" ou "Carlos #2")
            if m_out == mesa_nova and s_out == singer_nova:
                return (True, other_user, mesa_nova)
        return (False, "", mesa_nova)

    def is_song_in_queue(self, song_path: str) -> bool:
        """Check if a song is already in the queue."""
        return any(item["file"] == song_path for item in self.queue)

    def is_same_user_same_song_still_pending(self, song_path: str, user: str) -> bool:
        """Retorna True SE E SOMENTE SE: (mesmo user) adicionou (mesma musica) e ELA AINDA ESTA NA FILA
        (ou seja: ele ainda nao cantou essa musica). Qualquer outro caso -> False = LIBERADO.

        - MESMO USER, MESMA MUSICA, AINDA NA FILA (nao cantou) -> True  = BLOQUEIA
        - USUARIO DIFERENTE, MESMA MUSICA                     -> False = LIBERA
        - MESMO USER, MUSICA JA CANTOU (saiu da fila)        -> False = LIBERA
        - MESMO USER, OUTRA MUSICA                            -> False = fora do escopo
        """
        ukey = self._user_key(user)
        if not ukey:
            return False
        for item in self.queue:
            if item.get("file") == song_path and self._user_key(item.get("user", "")) == ukey:
                return True
        return False

    def is_user_limited(self, user: str) -> bool:
        """Check if a user has reached their queue limit."""
        limit = self._preferences.get_or_default("limit_user_songs_by")
        if limit == 0 or user in ("Pikaraoke", "Randomizer"):
            return False

        now_playing_user = self._get_now_playing_user() if self._get_now_playing_user else None
        user_key = self._user_key(user)
        count = sum(1 for item in self.queue if self._user_key(item["user"]) == user_key) + (
            1 if now_playing_user and self._user_key(now_playing_user) == user_key else 0
        )
        return count >= int(limit)

    def _resolve_title(self, song_path: str) -> str:
        """Get a display title from a song path."""
        if self._filename_from_path:
            return self._filename_from_path(song_path, True)
        return song_path

    def _find_song_index(self, song_path: str) -> int:
        """Find a song's index in the queue by exact path match. Returns -1 if not found."""
        for idx, item in enumerate(self.queue):
            if song_path == item["file"]:
                return idx
        return -1

    def _calculate_fair_queue_position(self, user: str) -> int:
        """Calculate insertion position using Nagle Fair Queuing.

        Users take turns in rounds: a user's Nth song is placed after all
        other users' Nth songs (or at queue end).
        """
        user_key = self._user_key(user)
        user_song_count = sum(1 for item in self.queue if self._user_key(item["user"]) == user_key)

        # Find position after the last song in "round N" where N = user_song_count
        # Round 0 = first song from each user, Round 1 = second song, etc.
        target_round = user_song_count
        songs_seen_per_user: dict[str, int] = {}

        for idx, item in enumerate(self.queue):
            queue_user = self._user_key(item["user"])
            songs_seen_per_user[queue_user] = songs_seen_per_user.get(queue_user, 0) + 1
            # This song is in round (count - 1) for its user
            song_round = songs_seen_per_user[queue_user] - 1
            if song_round == target_round:
                # Found a song in the target round, insert after it
                # Keep scanning to find the LAST song in this round
                pass
            elif song_round > target_round:
                # We've moved past target round, insert here
                return idx

        # All songs are in rounds <= target_round, append to end
        return len(self.queue)

    def enqueue(
        self,
        song_path: str,
        user: str = "Pikaraoke",
        semitones: int = 0,
        add_to_front: bool = False,
        log_action: bool = True,
    ) -> list[bool | str]:
        """Add a song to the queue. Returns [success, message]."""
        title = self._resolve_title(song_path)

        # REGRA NOVA: APENAS MESMO USUARIO + MESMA MUSICA + AINDA NA FILA (nao cantou ainda) -> BLOQUEIA
        # Qualquer outro caso (outro user quer a mesma musica, ou mesmo user ja cantou ela antes) -> LIBERA
        if self.is_same_user_same_song_still_pending(song_path, user):
            logging.warning(
                "Same user same song still pending in queue rejected: user=%s song=%s"
                % (str(user), str(song_path))
            )
            try:
                msg = _(
                    "Voce ja adicionou a musica '%s' e ela ainda esta na fila (ainda nao cantou). So pode repetir depois que cantar. Outros usuarios podem cantar a mesma musica normalmente."
                ) % str(title)
            except Exception:
                msg = (
                    f"Voce ja adicionou a musica '{title}' e ela ainda esta na fila (ainda nao cantou). "
                    f"So pode repetir depois que cantar. Outros usuarios podem cantar a mesma musica normalmente."
                )
            return [False, msg]

        # Regra: NOME IGUAL NA MESMA MESA -> Pede outro nome
        conflict, conflict_user_display, conflict_mesa = self.is_singer_name_already_in_same_mesa(user)
        if conflict:
            logging.warning(
                "Singer duplicate in same mesa rejected: user=%s conflict=%s mesa=%s song=%s"
                % (user, conflict_user_display, conflict_mesa, song_path)
            )
            m_label = conflict_mesa if conflict_mesa and conflict_mesa != "global" else _("local")
            try:
                msg = _(
                    "Ja existe um cantor com esse nome na mesa %s: '%s'. Por favor escolha outro nome (adicione um sobrenome ou apelido)."
                ) % (str(m_label), str(conflict_user_display))
            except Exception:
                msg = (
                    f"Ja existe um cantor com esse nome na mesa {m_label}: '{conflict_user_display}'. "
                    f"Por favor escolha outro nome."
                )
            return [False, msg]

        if self.is_user_limited(user):
            limit = self._preferences.get_or_default("limit_user_songs_by")
            logging.debug("User limited by: " + str(limit))
            return [
                False,
                _("You reached the limit of %s song(s) from an user in queue!") % (str(limit)),
            ]

        queue_item = {
            "user": user,
            "file": song_path,
            "title": title,
            "semitones": semitones,
        }
        if add_to_front:
            # MSG: Message shown after the song is added to the top of the queue
            self._events.emit(
                "notification",
                _("%s added to top of queue: %s") % (user, queue_item["title"]),
                "info",
            )
            self.queue.insert(0, queue_item)
        else:
            if log_action:
                # MSG: Message shown after the song is added to the queue
                self._events.emit(
                    "notification",
                    _("%s added to the queue: %s") % (user, queue_item["title"]),
                    "info",
                )
            if self._preferences.get_or_default("enable_fair_queue"):
                insert_pos = self._calculate_fair_queue_position(user)
                self.queue.insert(insert_pos, queue_item)
            else:
                self.queue.append(queue_item)
        self._events.emit("queue_update")
        self._events.emit("now_playing_update")
        return [
            True,
            _("Song added to the queue: %s") % title,
        ]

    def queue_add_random(self, amount: int) -> bool:
        """Add random songs to the queue. Returns False if ran out of songs."""
        logging.info("Adding %d random songs to queue" % amount)

        if not self._get_available_songs:
            logging.error("No available songs callback provided!")
            return False

        available_songs = self._get_available_songs()

        if not available_songs:
            logging.warning("No available songs!")
            return False

        # Get songs not already in queue
        queued_paths = {item["file"] for item in self.queue}
        eligible_songs = [s for s in available_songs if s not in queued_paths]

        if not eligible_songs:
            logging.warning("All songs are already in queue!")
            return False

        # Sample up to 'amount' songs (or all eligible if fewer available)
        sample_size = min(amount, len(eligible_songs))
        selected = random.sample(eligible_songs, sample_size)

        for song in selected:
            self.enqueue(song, "Randomizer")

        if sample_size < amount:
            logging.warning("Ran out of songs! Only added %d" % sample_size)
            return False

        return True

    def queue_clear(self) -> None:
        """Clear all songs from the queue and skip current song."""
        # MSG: Message shown after the queue is cleared
        self._events.emit("notification", _("Clear queue"), "danger")
        self.queue = []
        self._events.emit("queue_update")
        self._events.emit("now_playing_update")
        self._events.emit("skip_requested")

    def reorder(self, old_index: int, new_index: int) -> bool:
        """Move a song from old_index to new_index. Returns False if indices are invalid."""
        if not (0 <= old_index < len(self.queue) and 0 <= new_index < len(self.queue)):
            logging.error(
                f"Invalid reorder indices: old={old_index}, new={new_index}, queue_len={len(self.queue)}"
            )
            return False

        if old_index == new_index:
            return True

        item = self.queue.pop(old_index)
        self.queue.insert(new_index, item)
        logging.info(f"Reordered queue: moved index {old_index} to {new_index}")
        self._events.emit("queue_update")
        self._events.emit("now_playing_update")
        return True

    def move_to_top(self, song_path: str) -> bool:
        """Move a song to the top of the queue (index 0). Returns False if not found or already at top."""
        index = self._find_song_index(song_path)
        if index < 1:  # Not found (-1) or already at top (0)
            if index == -1:
                logging.error("Song not found in queue: " + song_path)
            else:
                logging.warning("Song is already at top of queue: " + song_path)
            return False
        return self.reorder(index, 0)

    def move_to_bottom(self, song_path: str) -> bool:
        """Move a song to the bottom of the queue. Returns False if not found or already at bottom."""
        index = self._find_song_index(song_path)
        if index < 0:
            logging.error("Song not found in queue: " + song_path)
            return False
        if index >= len(self.queue) - 1:
            logging.warning("Song is already at bottom of queue: " + song_path)
            return False
        return self.reorder(index, len(self.queue) - 1)

    def pop_next(self) -> dict[str, Any] | None:
        """Remove and return the next song from the queue.

        Does not emit queue_update to avoid UI flicker during song transitions.
        The playback system emits now_playing events which trigger queue UI updates.

        Returns None if queue is empty.
        """
        if not self.queue:
            return None

        if not self._preferences.get_or_default("enable_fair_queue"):
            song = self.queue.pop(0)
            self._last_popped_user_key = self._user_key(song["user"])
            self._round_seen_user_keys = {self._last_popped_user_key}
            logging.info(f"Popped song from queue: {song['title']}")
            return song

        user_keys_in_queue = {self._user_key(item["user"]) for item in self.queue}
        if self._round_seen_user_keys.issuperset(user_keys_in_queue):
            self._round_seen_user_keys = set()

        last_key = self._last_popped_user_key
        has_other_user = (
            last_key is not None and any(self._user_key(item["user"]) != last_key for item in self.queue)
        )
        has_unseen_user = any(
            self._user_key(item["user"]) not in self._round_seen_user_keys for item in self.queue
        )

        def is_candidate(item: dict[str, Any]) -> bool:
            k = self._user_key(item["user"])
            if has_other_user and last_key is not None and k == last_key:
                return False
            if has_unseen_user and k in self._round_seen_user_keys:
                return False
            return True

        candidate_index = next((i for i, it in enumerate(self.queue) if is_candidate(it)), None)
        if candidate_index is None and has_other_user:
            candidate_index = next(
                (i for i, it in enumerate(self.queue) if self._user_key(it["user"]) != last_key),
                None,
            )
        if candidate_index is None:
            candidate_index = 0

        song = self.queue.pop(candidate_index)
        self._last_popped_user_key = self._user_key(song["user"])
        self._round_seen_user_keys.add(self._last_popped_user_key)
        logging.info(f"Popped song from queue: {song['title']}")
        return song

    def queue_edit(self, song_path: str, action: str) -> bool:
        """Move or remove a song in the queue. Action: 'up', 'down', or 'delete'."""
        index = self._find_song_index(song_path)
        if index == -1:
            logging.error("Song not found in queue: " + song_path)
            return False

        if action == "up":
            if index < 1:
                logging.warning("Song is up next, can't bump up in queue: " + song_path)
                return False
            return self.reorder(index, index - 1)

        if action == "down":
            if index == len(self.queue) - 1:
                logging.warning("Song is already last, can't bump down in queue: " + song_path)
                return False
            return self.reorder(index, index + 1)

        if action == "delete":
            logging.info("Deleting song from queue: " + song_path)
            del self.queue[index]
            self._events.emit("queue_update")
            self._events.emit("now_playing_update")
            return True

        logging.error("Unrecognized action: " + action)
        return False
