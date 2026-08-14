"""Home page route."""

import flask_babel
from flask import make_response, render_template
from flask_smorest import Blueprint

from pikaraoke.lib.current_app import get_karaoke_instance, get_site_name, is_admin

_ = flask_babel.gettext


home_bp = Blueprint("home", __name__)


@home_bp.route("/")
def home():
    """Home page with now playing info and controls."""
    k = get_karaoke_instance()
    site_name = get_site_name()
    resp = make_response(render_template(
        "home.html",
        site_title=site_name,
        title="Home",
        transpose_value=k.playback_controller.now_playing_transpose,
        admin=is_admin(),
        is_transpose_enabled=k.is_transpose_enabled,
        volume=k.volume,
        mic_available=k.sound_manager.available,
    ))
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    resp.headers["Pragma"] = "no-cache"
    resp.headers["Expires"] = "0"
    return resp
