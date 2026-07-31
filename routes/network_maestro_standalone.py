"""Standalone Network Maestro routes."""

from flask import Blueprint, jsonify, render_template, request, url_for

from pikaraoke.lib.dell_wifi_sync import (
    DellWifiSyncError,
    connect_dell_wifi_network,
    disconnect_dell_wifi_network,
)
from pikaraoke.lib.network_maestro import get_network_maestro_contract, get_network_maestro_status
from pikaraoke.lib.wifi_setup import (
    WifiSetupError,
    connect_wifi_network,
    disconnect_wifi_network,
    scan_wifi_networks,
)

network_maestro_standalone_bp = Blueprint("network_maestro_standalone", __name__)


@network_maestro_standalone_bp.route("/")
def index():
    """Render the standalone operator panel."""
    return render_template("network_maestro_standalone.html", title="Maestro de Rede")


@network_maestro_standalone_bp.route("/health")
def health():
    """Simple health endpoint for service supervision."""
    return jsonify({"ok": True})


@network_maestro_standalone_bp.route("/api/status")
def status():
    """Return live status used by the standalone panel."""
    status_payload = get_network_maestro_status()
    base_url = request.url_root.rstrip("/")
    status_payload["access"] = {
        "panel_url": f"{base_url}{url_for('network_maestro_standalone.index')}",
        "health_url": f"{base_url}{url_for('network_maestro_standalone.health')}",
    }
    return jsonify(status_payload)


@network_maestro_standalone_bp.route("/api/contract")
def contract():
    """Return a compact machine-friendly contract for portal integration."""
    return jsonify(get_network_maestro_contract())


@network_maestro_standalone_bp.route("/api/wifi/networks")
def wifi_networks():
    """List nearby Wi-Fi networks."""
    rescan = request.args.get("rescan", "1") != "0"
    try:
        return jsonify({"networks": scan_wifi_networks(rescan=rescan)})
    except WifiSetupError as exc:
        return jsonify({"error": str(exc)}), 400


@network_maestro_standalone_bp.route("/api/wifi/connect", methods=["POST"])
def wifi_connect():
    """Connect the Raspberry Pi and sync the Dell to the same Wi-Fi network."""
    payload = request.get_json(silent=True) or request.form
    ssid = str(payload.get("ssid", "")).strip()
    password = str(payload.get("password", "")).strip()

    try:
        raspberry_result = connect_wifi_network(ssid, password or None)
    except WifiSetupError as exc:
        return jsonify({"success": False, "error": str(exc)}), 400

    try:
        dell_result = connect_dell_wifi_network(ssid, password or None)
    except DellWifiSyncError as exc:
        return (
            jsonify(
                {
                    "success": False,
                    "error": f"{raspberry_result.get('message', 'Raspberry conectado.')} Mas o Dell nao acompanhou: {exc}",
                    "raspberry": raspberry_result,
                }
            ),
            502,
        )

    return jsonify(
        {
            "success": True,
            "message": f"{raspberry_result.get('message', '')} {dell_result.get('message', '')}".strip(),
            "raspberry": raspberry_result,
            "dell": dell_result,
        }
    )


@network_maestro_standalone_bp.route("/api/wifi/disconnect", methods=["POST"])
def wifi_disconnect():
    """Disconnect Raspberry and Dell Wi-Fi while keeping the cable access alive."""
    try:
        raspberry_result = disconnect_wifi_network()
    except WifiSetupError as exc:
        return jsonify({"success": False, "error": str(exc)}), 400

    try:
        dell_result = disconnect_dell_wifi_network()
    except DellWifiSyncError as exc:
        return (
            jsonify(
                {
                    "success": False,
                    "error": f"{raspberry_result.get('message', 'Wi-Fi do Raspberry desconectado.')} Mas o Dell nao acompanhou: {exc}",
                    "raspberry": raspberry_result,
                }
            ),
            502,
        )

    return jsonify(
        {
            "success": True,
            "message": f"{raspberry_result.get('message', '')} {dell_result.get('message', '')}".strip(),
            "raspberry": raspberry_result,
            "dell": dell_result,
        }
    )
