"""Network Maestro panel routes."""

import flask_babel
from flask import current_app, jsonify, render_template, request, url_for
from flask_smorest import Blueprint

from pikaraoke.lib.network_maestro import get_network_maestro_status

_ = flask_babel.gettext

network_maestro_bp = Blueprint("network_maestro", __name__)


@network_maestro_bp.route("/network-maestro")
def network_maestro():
    """Render the live network operator panel."""
    wifi_setup_available = "wifi_setup.wifi_setup" in current_app.view_functions
    return render_template(
        "network_maestro.html",
        title="Maestro de Rede",
        admin=False,
        wifi_setup_available=wifi_setup_available,
        wifi_networks_url=url_for("wifi_setup.wifi_networks") if wifi_setup_available else "",
        wifi_connect_url=url_for("wifi_setup.wifi_connect") if wifi_setup_available else "",
        wifi_disconnect_url=url_for("wifi_setup.wifi_disconnect") if wifi_setup_available else "",
    )


@network_maestro_bp.route("/network-maestro/status")
def network_maestro_status():
    """Return live status used by the network operator panel."""
    status = get_network_maestro_status()
    base_url = request.url_root.rstrip("/")
    access = {
        "panel_url": f"{base_url}{url_for('network_maestro.network_maestro')}",
        "splash_url": f"{base_url}{url_for('splash.splash')}",
        "queue_url": f"{base_url}{url_for('queue.queue')}",
        "info_url": f"{base_url}{url_for('info.info')}",
    }
    if "wifi_setup.wifi_setup" in current_app.view_functions:
        access["wifi_setup_url"] = f"{base_url}{url_for('wifi_setup.wifi_setup')}"
    status["access"] = access
    return jsonify(status)
