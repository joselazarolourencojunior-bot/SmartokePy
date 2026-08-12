"""
Routes ABA NOVA 'Cantores' - FASE 1 + FASE 2 (AMARRACAO COM FILA REAL).
Expõe:
  GET  /singers              -> UI (HTML) lista cantores
  GET  /api/singers          -> JSON com todos cantores (para polling JS)
  GET  /api/singers/match_status -> JSON com cantores tocando agora / proximo / nao linkados
  POST /api/singers/add      -> JSON: {"name":"Lazaro", "note":"Mesa 2", "status":"pending_music"}
  POST /api/singers/update   -> JSON: {"singer_id":"sg_xxx", "status":"waiting"|...}
  POST /api/singers/note     -> JSON: {"singer_id":"sg_xxx", "note":"Nova anotação"}
  POST /api/singers/aliases  -> JSON: {"singer_id":"sg_xxx", "csv":"apelido1,apelido2,apelido3"}
  POST /api/singers/alias_add     -> JSON: {"singer_id":"sg_xxx", "alias":"José Mesa 1"}
  POST /api/singers/alias_remove  -> JSON: {"singer_id":"sg_xxx", "alias":"José Mesa 1"}
  POST /api/singers/remove   -> JSON: {"singer_id":"sg_xxx"}
  POST /api/singers/clear    -> JSON: {} (limpa tudo, confirmar no front)
"""
from __future__ import annotations

import logging

import flask_babel
from flask import jsonify, make_response, render_template, request
from flask_smorest import Blueprint

from pikaraoke.lib.current_app import get_karaoke_instance, get_site_name, is_admin

_ = flask_babel.gettext

log = logging.getLogger(__name__)

singers_bp = Blueprint("singers", __name__)


def _get_singer_manager():
    k = get_karaoke_instance()
    return k.singer_manager


def _get_queue_manager():
    k = get_karaoke_instance()
    return k.queue_manager


def _broadcast_singers():
    """Emite socketio events de singers atualizados + pending_calls + match_status (FASE 2)."""
    try:
        from pikaraoke.app import socketio
        sm = _get_singer_manager()
        socketio.emit("singers_updated", sm.to_dict())
        pending = sm.get_pending_calls()
        socketio.emit("singers_pending_calls", {
            "count": len(pending),
            "singers": [{"singer_id": s.singer_id, "name": s.name} for s in pending],
        })
        # FASE 2: match status (who is next, who is now playing, unmatched)
        try:
            qm = _get_queue_manager()
            k = get_karaoke_instance()
            np_info = k.get_now_playing() if hasattr(k, "get_now_playing") else {}
            queue = list(getattr(qm, "queue", []) or [])
            info = sm.refresh_statuses_from_queue(
                queue_items=queue,
                now_playing_user=np_info.get("now_playing_user"),
                next_user=np_info.get("next_user"),
            )
            socketio.emit("singers_match_status", info)
        except Exception:
            pass
    except Exception:
        pass


@singers_bp.route("/singers")
def singers():
    """Pagina UI (ABA NOVA) - Lista de cantores ordem chegada."""
    return make_response(render_template(
        "singers.html",
        site_title=get_site_name(),
        title=_("Cantores (Ordem de Chegada)"),
        admin=is_admin(),
    ))


@singers_bp.route("/api/singers", methods=["GET"])
def api_singers_list():
    sm = _get_singer_manager()
    return jsonify(sm.to_dict(include_left_early=True))


@singers_bp.route("/api/singers/picker_options", methods=["GET"])
def api_singers_picker_options():
    """Retorna JSON MESCLADO para o PICKER MODAL (select nome clicavel).

    Combina 2 fontes:
      1) CANTOS CADASTRADOS na aba Cantores (nome principal + cada apelido separado).
      2) NOMES USADOS RECENTEMENTE NA FILA REAL (queue_manager.queue -> campo user / song_added_by)
         que AINDA NAO foram linkados a nenhum cantor (unmatched).

    Estrutura de retorno:
    {
      "registered_singers": [ { "singer_id": str, "name": str, "aliases_display": [str,...] }, ...],
      "unmatched_queue_users": [ { "user": str, "norm": str, "count_songs": int }, ... ],
      "generated_at": float
    }
    """
    sm = _get_singer_manager()
    qm = _get_queue_manager()
    try:
        singers_raw = sm.to_dict(include_left_early=True)
        registered = []
        for s in singers_raw.get("singers", []) or []:
            registered.append({
                "singer_id": s.get("singer_id",""),
                "name": s.get("name",""),
                "aliases_display": list(s.get("aliases_display") or s.get("aliases") or []) or [],
                "status": s.get("status", "")
            })
        queue = list(getattr(qm, "queue", []) or [])
        unmatched = sm.get_unmatched_queue_users(queue) or []
        import json as _json
        resp = {
            "registered_singers": registered,
            "unmatched_queue_users": unmatched,
            "generated_at": singers_raw.get("generated_at"),
        }
        return (_json.dumps(resp, ensure_ascii=False), 200, {"Content-Type": "application/json"})
    except Exception as e:
        import json as _json
        return (_json.dumps({"error": str(e)}, ensure_ascii=False), 500, {"Content-Type": "application/json"})


@singers_bp.route("/api/singers/match_status", methods=["GET"])
def api_singers_match_status():
    """FASE 2: retorna {now_singer_id, next_singer_id, unmatched, changed} a partir da FILA REAL agora."""
    sm = _get_singer_manager()
    try:
        qm = _get_queue_manager()
        k = get_karaoke_instance()
        np_info = k.get_now_playing() if hasattr(k, "get_now_playing") else {}
        queue = list(getattr(qm, "queue", []) or [])
        info = sm.refresh_statuses_from_queue(
            queue_items=queue,
            now_playing_user=np_info.get("now_playing_user"),
            next_user=np_info.get("next_user"),
        )
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500
    return jsonify({"ok": True, **info})


@singers_bp.route("/api/singers/add", methods=["POST"])
def api_singers_add():
    data = request.get_json(silent=True) or request.form
    name = (data.get("name") or "").strip()
    note = (data.get("note") or "").strip()
    status = (data.get("status") or "pending_music").strip()
    if not name:
        return jsonify({"ok": False, "error": "Nome vazio"}), 400
    sm = _get_singer_manager()
    try:
        singer = sm.add_singer(name=name, note=note, initial_status=status)
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    _broadcast_singers()
    return jsonify({"ok": True, "singer_id": singer.singer_id, "list": sm.to_dict()})


@singers_bp.route("/api/singers/update", methods=["POST"])
def api_singers_update_status():
    data = request.get_json(silent=True) or request.form
    sid = (data.get("singer_id") or "").strip()
    status = (data.get("status") or "").strip()
    if not sid or not status:
        return jsonify({"ok": False, "error": "Missing fields"}), 400
    sm = _get_singer_manager()
    singer = sm.update_status(sid, status)
    if singer is None:
        return jsonify({"ok": False, "error": "Cantor nao encontrado ou status invalido"}), 404
    _broadcast_singers()
    return jsonify({"ok": True, "list": sm.to_dict()})


@singers_bp.route("/api/singers/note", methods=["POST"])
def api_singers_note():
    data = request.get_json(silent=True) or request.form
    sid = (data.get("singer_id") or "").strip()
    note = (data.get("note") or "").strip()
    if not sid:
        return jsonify({"ok": False, "error": "missing singer_id"}), 400
    sm = _get_singer_manager()
    singer = sm.update_note(sid, note)
    if singer is None:
        return jsonify({"ok": False, "error": "not found"}), 404
    _broadcast_singers()
    return jsonify({"ok": True, "list": sm.to_dict()})


@singers_bp.route("/api/singers/aliases", methods=["POST"])
def api_singers_aliases_csv():
    """FASE 2: Setar TODOS os apelidos de um cantor via CSV (virgula ou ;)."""
    data = request.get_json(silent=True) or request.form
    sid = (data.get("singer_id") or "").strip()
    csv = (data.get("csv") or "")
    if not sid:
        return jsonify({"ok": False, "error": "missing singer_id"}), 400
    sm = _get_singer_manager()
    singer = sm.set_aliases_from_csv(sid, csv)
    if singer is None:
        return jsonify({"ok": False, "error": "not found"}), 404
    _broadcast_singers()
    return jsonify({"ok": True, "list": sm.to_dict()})


@singers_bp.route("/api/singers/alias_add", methods=["POST"])
def api_singers_alias_add():
    data = request.get_json(silent=True) or request.form
    sid = (data.get("singer_id") or "").strip()
    alias = (data.get("alias") or "").strip()
    if not sid or not alias:
        return jsonify({"ok": False, "error": "missing singer_id/alias"}), 400
    sm = _get_singer_manager()
    singer = sm.add_alias(sid, alias)
    if singer is None:
        return jsonify({"ok": False, "error": "not found"}), 404
    _broadcast_singers()
    return jsonify({"ok": True, "list": sm.to_dict()})


@singers_bp.route("/api/singers/alias_remove", methods=["POST"])
def api_singers_alias_remove():
    data = request.get_json(silent=True) or request.form
    sid = (data.get("singer_id") or "").strip()
    alias = (data.get("alias") or "").strip()
    if not sid or not alias:
        return jsonify({"ok": False, "error": "missing singer_id/alias"}), 400
    sm = _get_singer_manager()
    singer = sm.remove_alias(sid, alias)
    if singer is None:
        return jsonify({"ok": False, "error": "not found"}), 404
    _broadcast_singers()
    return jsonify({"ok": True, "list": sm.to_dict()})


@singers_bp.route("/api/singers/remove", methods=["POST"])
def api_singers_remove():
    data = request.get_json(silent=True) or request.form
    sid = (data.get("singer_id") or "").strip()
    if not sid:
        return jsonify({"ok": False, "error": "missing singer_id"}), 400
    sm = _get_singer_manager()
    ok = sm.remove_singer(sid)
    _broadcast_singers()
    return jsonify({"ok": ok, "list": sm.to_dict()})


@singers_bp.route("/api/singers/clear", methods=["POST"])
def api_singers_clear():
    sm = _get_singer_manager()
    sm.clear_all()
    _broadcast_singers()
    return jsonify({"ok": True, "list": sm.to_dict()})
