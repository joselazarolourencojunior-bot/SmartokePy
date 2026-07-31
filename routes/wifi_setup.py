"""Visual Wi-Fi setup assistant for Raspberry Pi systems."""

import flask_babel
from flask import jsonify, render_template, request
from flask_smorest import Blueprint

from pikaraoke.lib.wifi_setup import (
    WifiSetupError,
    connect_wifi_network,
    disconnect_wifi_network,
    get_wifi_status,
    scan_wifi_networks,
)

_ = flask_babel.gettext

wifi_setup_bp = Blueprint("wifi_setup", __name__)


@wifi_setup_bp.route("/wifi-setup")
def wifi_setup():
    """Render the Wi-Fi assistant page."""
    return render_template("wifi_setup.html", title="Assistente Wi-Fi", admin=False)


@wifi_setup_bp.route("/wifi-setup/status")
def wifi_status():
    """Return the current Raspberry Pi Wi-Fi status."""
    try:
        return jsonify(get_wifi_status())
    except WifiSetupError as exc:
        return jsonify({"error": str(exc)}), 400


@wifi_setup_bp.route("/wifi-setup/networks")
def wifi_networks():
    """List nearby Wi-Fi networks."""
    rescan = request.args.get("rescan", "1") != "0"
    try:
        return jsonify({"networks": scan_wifi_networks(rescan=rescan)})
    except WifiSetupError as exc:
        return jsonify({"error": str(exc)}), 400


@wifi_setup_bp.route("/wifi-setup/connect", methods=["POST"])
def wifi_connect():
    """Connect the Raspberry Pi to a Wi-Fi network."""
    payload = request.get_json(silent=True) or request.form
    ssid = str(payload.get("ssid", "")).strip()
    password = str(payload.get("password", "")).strip()

    try:
        result = connect_wifi_network(ssid, password or None)
        return jsonify({"success": True, **result})
    except WifiSetupError as exc:
        return jsonify({"success": False, "error": str(exc)}), 400


@wifi_setup_bp.route("/wifi-setup/disconnect", methods=["POST"])
def wifi_disconnect():
    """Disconnect the Raspberry Pi from the current Wi-Fi network."""
    try:
        result = disconnect_wifi_network()
        return jsonify({"success": True, **result})
    except WifiSetupError as exc:
        return jsonify({"success": False, "error": str(exc)}), 400
