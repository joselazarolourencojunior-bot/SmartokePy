"""Song queue management routes."""

from __future__ import annotations

import json
import logging
from urllib.parse import unquote

import flask_babel
from flask import flash, make_response, redirect, render_template, request, url_for
from flask_smorest import Blueprint
from marshmallow import Schema, fields

from pikaraoke.lib.current_app import (
    broadcast_event,
    get_karaoke_instance,
    get_site_name,
    is_admin,
)

_ = flask_babel.gettext

queue_bp = Blueprint("queue", __name__)


class ReorderForm(Schema):
    old_index = fields.Integer(
        required=True, metadata={"description": "Current index of the item to move"}
    )
    new_index = fields.Integer(
        required=True, metadata={"description": "Target index to move the item to"}
    )


class EnqueueQuery(Schema):
    song = fields.String(required=True, metadata={"description": "Path to the song file"})
    user = fields.String(
        load_default="", metadata={"description": "Name of the user adding the song"}
    )


class EnqueueForm(Schema):
    song_to_add = fields.String(required=True, metadata={"description": "Path to the song file"})
    song_added_by = fields.String(
        load_default="", metadata={"description": "Name of the user adding the song"}
    )


class QueueEditQuery(Schema):
    action = fields.String(required=True, metadata={"description": "Queue edit action to perform"})
    song = fields.String(
        metadata={"description": "Path to the song file (required unless action is 'clear')"}
    )


@queue_bp.route("/queue")
def queue():
    """Queue management page."""
    k = get_karaoke_instance()
    site_name = get_site_name()
    resp = make_response(render_template(
        "queue.html",
        queue=k.queue_manager.queue,
        site_title=site_name,
        title="Queue",
        admin=is_admin(),
    ))
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    resp.headers["Pragma"] = "no-cache"
    resp.headers["Expires"] = "0"
    return resp


@queue_bp.route("/get_queue")
def get_queue():
    """Get the current song queue.

    Garante que todos itens tenham `qid` (para mapping status preload na UI),
    mantendo compatibilidade com itens criados antes da feature de janela multi-preload.
    """
    k = get_karaoke_instance()
    from pikaraoke.lib.queue_manager import QueueManager as _QM
    for item in k.queue_manager.queue:
        _QM.ensure_item_qid(item)
    return json.dumps(k.queue_manager.queue)


@queue_bp.route("/api/preload_status_map")
def preload_status_map():
    """Mapa de status de preload POR qid. Usado pelo Maestro para colorir LEDs.

    Returns JSON:
      {
        "by_qid": { "<qid>": "ready" | "transcoding" | "queued" | "failed" | "unknown" },
        "labels": {"ready":"Pronta...", "transcoding":"Carregando..."},
        "colors": {"ready":"#34d399", ...},
        "window_size": 5,
        "max_parallel": 1,
        "running_count": 1, "ready_count": 2, "queued_count": 0, "failed_count": 0,
      }
    """
    k = get_karaoke_instance()
    payload = k.playback_controller.get_preload_status_map(include_meta=True)
    return json.dumps(payload)


@queue_bp.route("/queue/addrandom/<int:amount>", methods=["GET"])
def add_random(amount):
    """Add random songs to the queue."""
    if not is_admin():
        flash(_("You don't have permission to add random songs"), "is-danger")
        return redirect(url_for("queue.queue"))
    k = get_karaoke_instance()
    rc = k.queue_manager.queue_add_random(amount)
    if rc:
        # MSG: Message shown after adding random tracks
        flash(_("Added %s random tracks") % amount, "is-success")
    else:
        # MSG: Message shown after running out songs to add during random track addition
        flash(_("Ran out of songs!"), "is-warning")
    broadcast_event("queue_update")
    return redirect(url_for("queue.queue"))


@queue_bp.route("/queue/addrandom_api/<int:amount>", methods=["GET"])
def add_random_api(amount):
    """Add random songs to the queue. Returns JSON (nao faz redirect full page, evita
    'conexao perdida' no SocketIO do SPA hijack).
    """
    if not is_admin():
        return (
            json.dumps(
                {"ok": False, "error": _("You don't have permission to add random songs")}
            ),
            403,
            {"Content-Type": "application/json"},
        )
    amount = max(1, min(999, int(amount)))
    k = get_karaoke_instance()
    added = 0
    try:
        # queue_manager.queue_add_random retorna quantas musicas foram adicionadas?
        # O core original retorna True/False. Contamos antes/depois via len(queue)
        before = len(k.queue_manager.queue) if hasattr(k, "queue_manager") else 0
        rc = k.queue_manager.queue_add_random(amount)
        after = len(k.queue_manager.queue) if hasattr(k, "queue_manager") else 0
        added = max(0, after - before)
        if rc and added <= 0:
            added = amount  # fallback se calculo errado
    except Exception as e:
        return (
            json.dumps({"ok": False, "error": "Erro interno: %s" % e}),
            500,
            {"Content-Type": "application/json"},
        )
    broadcast_event("queue_update")
    if rc:
        return (
            json.dumps(
                {
                    "ok": True,
                    "added": added,
                    "message": _("Added %s random tracks") % added,
                    "ran_out": False,
                }
            ),
            200,
            {"Content-Type": "application/json"},
        )
    return (
        json.dumps(
            {
                "ok": False,
                "added": added,
                "error": _("Ran out of songs!"),
                "ran_out": True,
            }
        ),
        200,
        {"Content-Type": "application/json"},
    )


@queue_bp.route("/queue/reorder", methods=["POST"])
@queue_bp.arguments(ReorderForm, location="form")
def reorder(form):
    """Handle drag-and-drop reordering of the queue."""
    if not is_admin():
        return json.dumps({"success": False, "error": "Unauthorized"}), 403

    k = get_karaoke_instance()
    try:
        success = k.queue_manager.reorder(form["old_index"], form["new_index"])
        return json.dumps({"success": success})
    except (ValueError, IndexError):
        pass

    return json.dumps({"success": False})


@queue_bp.route("/queue/edit", methods=["GET"])
@queue_bp.arguments(QueueEditQuery, location="query")
def queue_edit(query):
    """Edit queue items (admin only)."""
    is_ajax = request.headers.get("X-Requested-With") == "XMLHttpRequest"

    if not is_admin():
        if is_ajax:
            return json.dumps({"success": False, "error": "Unauthorized"}), 403
        # MSG: Message shown when non-admin tries to edit queue
        flash(_("Unauthorized"), "is-danger")
        return redirect(url_for("queue.queue"))

    k = get_karaoke_instance()
    action = query["action"]
    success = False
    message = ""

    if action == "clear":
        k.queue_manager.queue_clear()
        message = _("Cleared the queue!")
        if not is_ajax:
            # MSG: Message shown after clearing the queue
            flash(message, "is-warning")
        broadcast_event("skip", "clear queue")
        success = True
    else:
        song = unquote(query.get("song", ""))
        song_title = k.song_manager.display_name_from_path(song)

        # MSG labels for each action
        success_labels = {
            "top": _("Moved to top of queue"),
            "bottom": _("Moved to bottom of queue"),
            "up": _("Moved up in queue"),
            "down": _("Moved down in queue"),
            "delete": _("Deleted from queue"),
        }
        error_labels = {
            "top": _("Error moving to top of queue"),
            "bottom": _("Error moving to bottom of queue"),
            "up": _("Error moving up in queue"),
            "down": _("Error moving down in queue"),
            "delete": _("Error deleting from queue"),
        }

        if action == "top":
            success = k.queue_manager.move_to_top(song)
        elif action == "bottom":
            success = k.queue_manager.move_to_bottom(song)
        else:
            success = k.queue_manager.queue_edit(song, action)

        if action in success_labels:
            message = (
                (success_labels[action] if success else error_labels[action]) + ": " + song_title
            )

        if message and not is_ajax:
            flash(message, "is-success" if success else "is-danger")

    # Note: No need to manually emit events here - all QueueManager methods
    # (queue_clear, queue_edit, reorder) already emit queue_update and now_playing_update events

    if is_ajax:
        return json.dumps({"success": success, "message": message})
    return redirect(url_for("queue.queue"))


def _do_enqueue(song: str, user: str) -> str:
    k = get_karaoke_instance()
    rc = k.queue_manager.enqueue(song, user)
    broadcast_event("queue_update")
    # rc = [success: bool, message: str]
    try:
        ok = bool(rc[0]) if isinstance(rc, (list, tuple)) and len(rc) > 0 else False
    except Exception:
        ok = False
    if ok:
        song_title = k.song_manager.display_name_from_path(song)
        return json.dumps({"song": song_title, "success": True, "message": ""})
    msg_val = str(rc[1]) if isinstance(rc, (list, tuple)) and len(rc) > 1 else (
        "Não foi possível adicionar a música à fila (motivo desconhecido)."
    )
    logging.warning("[enqueue_rejected V9] user=%s song=%r msg=%s", user, song, msg_val[:300])
    try:
        song_title = k.song_manager.display_name_from_path(song)
    except Exception:
        song_title = song or "(sem nome)"
    return json.dumps({"song": song_title, "success": False, "message": msg_val})


@queue_bp.route("/enqueue", methods=["GET"])
@queue_bp.arguments(EnqueueQuery, location="query")
def enqueue(query):
    """Add a song to the queue (used by the file browser)."""
    return _do_enqueue(query["song"], query["user"])


@queue_bp.route("/enqueue", methods=["POST"])
@queue_bp.arguments(EnqueueForm, location="form")
def enqueue_form(form):
    """Add a song to the queue (used by the search page)."""
    return _do_enqueue(form["song_to_add"], form["song_added_by"])


@queue_bp.route("/queue/downloads")
def get_current_downloads():
    """Get the status of current and pending downloads.

    GARANTIA PT-BR TOTAL: qualquer mensagem de erro que chegar aqui seja em INGLES cru
    (ex: "Already exists in library" antigo, "corrupted file", "video restricted" etc.)
    passa pelo tradutor do DownloadManager antes de retornar JSON para o frontend.
    Se por algum motivo a instancia do manager nao tiver o metodo, usamos mapa inline fallback.
    """
    k = get_karaoke_instance()
    status = k.download_manager.get_downloads_status()
    errors = status.get("errors") or []
    # Tenta usar o tradutor nativo da classe
    translate_fn = None
    try:
        translate_fn = getattr(k.download_manager, "_translate_error_to_ptbr", None)
    except Exception:
        translate_fn = None
    # Fallback map (caso a instancia antiga nao tenha metodo carregado ainda por cache)
    PTBR_FALLBACK_MAP: list[tuple[tuple[str, ...], str]] = [
        (("already exists in library",),
         "Música já existia na biblioteca local (não precisa baixar de novo)"),
        (("does not look like karaoke", "rejected non-karaoke", "rejected because title does not look like karaoke"),
         "Esta música NÃO É KARAOKÊ (o título ou canal não indica versão karaokê — use busca karaokê ou adicione '[Karaoke]' no nome se tiver certeza)"),
        (("corrupted or invalid file removed", "integrity check", "failed integrity check"),
         "Arquivo corrompido ou inválido (download quebrou no meio e foi apagado automaticamente — tente baixar de novo)"),
        (("saved file could not be found", "download finished but the saved file could not be found",
          "could not find downloaded song", "could not be located"),
         "Download terminou mas o arquivo salvo NÃO FOI ENCONTRADO na pasta das músicas (disco cheio ou caminho inválido)"),
        (("download timed out and was cancelled", "stalled for more than", "yt-dlp stalled"),
         "Download PAROU no meio (timeout) — internet caiu ou travou. Tente novamente mais tarde"),
        (("video unavailable", "private video", "this video is private", "sign in to confirm your age",
          "not available in your country", "members-only", "copyright", "restricted", "blocked"),
         "Vídeo BLOQUEADO ou PRIVADO (idade, país, membro do canal, direitos autorais ou o dono retirou)"),
        (("http error 403", "http error 429", "too many requests", "rate-limited", "denied access"),
         "YouTube/link limitou seus downloads por excesso (HTTP 403/429). Aguarde 5~10 minutos e tente de novo"),
        (("timed out", "timeout", "network is unreachable", "temporary failure in name resolution",
          "connection refused", "connection reset", "no route to host", "network error"),
         "ERRO DE REDE / INTERNET (wi-fi caiu, DNS ou roteador). Verifique a conexão com a Internet"),
        (("permission denied", "read-only file system", "no space left on device", "storage error", "disk full"),
         "ERRO AO SALVAR NO DISCO (disco cheio ou sem permissão de gravação na pasta das músicas)"),
        (("unsupported url", "unsupported url scheme", "invalid video link", "invalid url"),
         "Link de vídeo INVÁLIDO ou não suportado (não é link do YouTube ou formato reconhecido)"),
    ]
    def _fallback_translate(raw: str) -> str:
        text = (raw or "").strip()
        if not text:
            return "Ocorreu um erro desconhecido durante o download (tente novamente)"
        low = text.casefold()
        for tokens, msg in PTBR_FALLBACK_MAP:
            if any(tok and tok in low for tok in tokens):
                tail = ""
                if ":" in text:
                    tail = text.split(":", 1)[1].strip()
                if tail and len(tail) <= 240 and tail not in msg:
                    return msg + ": " + tail
                return msg
        return text  # nao bateu nenhuma regra, volta original (frontend ainda tem fallback 2)

    for err in errors:
        raw_msg = str(err.get("error") or "")
        tail_original = ""
        if ":" in raw_msg:
            tail_original = raw_msg.split(":", 1)[1].strip()
        translated = raw_msg
        try:
            if translate_fn is not None:
                translated = translate_fn(raw_msg, existing_name=(tail_original or None)) or raw_msg
            else:
                translated = _fallback_translate(raw_msg) or raw_msg
        except Exception:
            try:
                translated = _fallback_translate(raw_msg) or raw_msg
            except Exception:
                translated = raw_msg
        # Nunca retorna mensagem vazia
        if not translated:
            translated = raw_msg or "Ocorreu um erro desconhecido durante o download"
        err["error"] = translated
    status["errors"] = errors
    return json.dumps(status)


@queue_bp.route("/queue/downloads/errors/<error_id>", methods=["DELETE"])
def delete_download_error(error_id):
    """Remove a download error from the list."""
    k = get_karaoke_instance()
    if k.download_manager.remove_error(error_id):
        return json.dumps({"success": True})
    return json.dumps({"success": False, "error": "Error not found"}), 404
