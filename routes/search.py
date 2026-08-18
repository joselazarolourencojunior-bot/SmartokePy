"""YouTube search and download routes."""

from __future__ import annotations

import json

import flask_babel
from flask import current_app, jsonify, render_template, request, url_for
from flask_smorest import Blueprint
from marshmallow import Schema, fields

from pikaraoke.lib.current_app import get_karaoke_instance, get_site_name
from pikaraoke.lib.youtube_dl import get_search_results, get_stream_url, normalize_youtube_url_to_std

_ = flask_babel.gettext

search_bp = Blueprint("search", __name__)


class AutocompleteQuery(Schema):
    q = fields.String(required=True, metadata={"description": "Search query for autocomplete"})


class PreviewQuery(Schema):
    url = fields.String(required=True, metadata={"description": "YouTube video URL to preview"})


class DownloadBody(Schema):
    song_url = fields.String(required=True, metadata={"description": "YouTube URL to download"})
    song_added_by = fields.String(
        load_default="Pikaraoke", metadata={"description": "Name of the user requesting the download"}
    )
    song_title = fields.String(
        load_default="", metadata={"description": "Display title for the song"}
    )
    queue = fields.Boolean(
        load_default=False, metadata={"description": "Whether to queue the song after download"}
    )


@search_bp.route("/search", methods=["GET"])
def search():
    """YouTube search page."""
    k = get_karaoke_instance()
    site_name = get_site_name()
    search_string = request.args.get("search_string")
    if search_string:
        non_karaoke = request.args.get("non_karaoke") == "true"
        search_results = get_search_results(search_string, karaoke_only=not non_karaoke)
    else:
        search_string = None
        search_results = None
    return render_template(
        "search.html",
        site_title=site_name,
        title="Search",
        songs=k.song_manager.songs,
        search_results=search_results,
        search_string=search_string,
    )


@search_bp.route("/autocomplete")
@search_bp.arguments(AutocompleteQuery, location="query")
def autocomplete(query):
    """Search available songs for autocomplete."""
    import re as _re

    def _extract_ytid_from_path(path: str):
        if not path:
            return None
        # Padrao nosso download: ---VIDEID.ext (ex: musica---boE0xfp7u2M.mp4)
        m = _re.search(r"---([a-zA-Z0-9_-]{11})(?:\.[a-zA-Z0-9]{2,6})?$", str(path))
        if m and m[1]:
            return m[1]
        # Padrao [VIDEID]
        m = _re.search(r"\[([a-zA-Z0-9_-]{11})\]", str(path))
        if m and m[1]:
            return m[1]
        # Padrao ?v=VIDEID (se path for URL)
        m = _re.search(r"[?&]v=([a-zA-Z0-9_-]{11})", str(path))
        if m and m[1]:
            return m[1]
        return None

    k = get_karaoke_instance()
    q = query["q"].lower()
    result = []
    for each in k.song_manager.songs:
        if q in each.lower():
            display = k.song_manager.display_name_from_path(each)
            ytid = _extract_ytid_from_path(each)
            result.append(
                {
                    "path": each,
                    "fileName": display,
                    "type": "autocomplete",
                    "ytid": ytid or "",
                }
            )
    response = current_app.response_class(response=json.dumps(result), mimetype="application/json")
    return response


@search_bp.route("/preview")
@search_bp.arguments(PreviewQuery, location="query")
def preview(query):
    """Get a direct stream URL for previewing a YouTube video."""
    stream_url = get_stream_url(query["url"])
    if stream_url is None:
        return jsonify({"error": "Could not fetch stream URL"}), 500
    return jsonify({"stream_url": stream_url})


@search_bp.route("/download", methods=["POST"])
@search_bp.arguments(DownloadBody, location="json")
def download(form):
    """Download a video from YouTube.

    [V52 REGRAS NOVAS USUARIO + HOTFIX URL DUPLICADA]
    1. Se title foi passado VAZIO -> LINK DIRETO (force_skip_analysis=True).
    2. SEMPRE normaliza a URL via youtube_dl.normalize_youtube_url_to_std(),
       para remover bug URL duplicada: watch?v=https://www.youtube.com/watch?v=XXXXXX
    """
    k = get_karaoke_instance()
    song = str(form.get("song_url") or "").strip()
    # V52 HOTFIX: NORMALIZA URL ANTES DE QUALQUER COISA. Evita bug URL duplicada.
    song = normalize_youtube_url_to_std(song) or song
    user = form.get("song_added_by") or "Pikaraoke"
    title = str(form.get("song_title") or "").strip()
    queue = form.get("queue", False)

    # Detectar LINK DIRETO:
    _url_norm = song
    _title_norm = title
    _is_direct_link = False
    if not _title_norm:
        _is_direct_link = True
    elif _title_norm == _url_norm:
        _is_direct_link = True
    elif _title_norm.lower().startswith("https://") or _title_norm.lower().startswith("http://"):
        _is_direct_link = True
    elif " " not in _title_norm and len(_title_norm) <= 15:
        _is_direct_link = True

    status, message = k.download_manager.queue_download(
        song,
        queue,
        user,
        title,
        force_skip_analysis=_is_direct_link,
    )

    return jsonify(
        {
            "status": status,
            "message": message,
        }
    )
