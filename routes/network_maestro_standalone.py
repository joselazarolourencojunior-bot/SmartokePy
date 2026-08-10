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
        return jsonify({"error": str(exc), "networks": []}), 200
    except Exception as exc:
        return jsonify({"error": str(exc), "networks": []}), 200


# ---------- Compatibilidade com portal React acess-karaoke ----------

@network_maestro_standalone_bp.route("/api/state")
def state_alias():
    """Alias para /api/status — usado pelo painel Operação do acess-karaoke."""
    return status()


@network_maestro_standalone_bp.route("/api/networks")
def networks_alias():
    """Alias para /api/wifi/networks com fallback seguro."""
    try:
        return wifi_networks()
    except Exception as exc:
        return jsonify({"networks": [], "error": str(exc)}), 200


@network_maestro_standalone_bp.route("/api/diagnostic")
def diagnostic():
    """Resumo agregado de diagnóstico para o painel de Operação."""
    import traceback

    result = {"checks": {}, "ok": True, "errors": []}
    try:
        st = get_network_maestro_status()
        result["checks"]["internet"] = {
            "online": bool((st.get("internet") or {}).get("online")),
            "message": (st.get("internet") or {}).get("message", ""),
        }
        cc = st.get("control_channel") or {}
        result["checks"]["control_channel"] = {
            "ready": bool(cc.get("ready")),
            "mode": cc.get("mode", ""),
            "primary_ip": (cc.get("primary") or {}).get("ipv4", ""),
            "message": cc.get("message", ""),
        }
        ng = st.get("ngrok") or {}
        result["checks"]["ngrok"] = {
            "online": bool(ng.get("online")),
            "public_url": ng.get("public_url", ""),
            "message": ng.get("message", ""),
        }
        try:
            nets = scan_wifi_networks(rescan=False) or []
            result["checks"]["wifi_scan"] = {"ok": True, "count": len(nets)}
        except Exception as exc:
            result["checks"]["wifi_scan"] = {"ok": False, "error": str(exc)}
            result["ok"] = False
            result["errors"].append(f"wifi_scan: {exc}")
    except Exception as exc:
        result["ok"] = False
        result["errors"].append(f"status_base: {exc}")
        result["traceback"] = traceback.format_exc()
    return jsonify(result)


@network_maestro_standalone_bp.route("/api/public-urls")
def public_urls():
    """URLs locais e túneis (ngrok + Cloudflare) para o painel."""
    st = get_network_maestro_status()
    base_url = request.url_root.rstrip("/")
    ng = st.get("ngrok") or {}
    out = {
        "local": {
            "pikaraoke": base_url,
            "maestro": f"{base_url}/maestro/",
            "splash": f"{base_url}/splash",
            "queue": f"{base_url}/queue",
        },
        "tunnels": {
            "ngrok": ng.get("public_url", "") if ng.get("online") else "",
            # Cloudflare Tunnels (documentados no RUNBOOK)
            "portal_thermowatch": "https://portal.thermowatch.com.br",
            "karaoke_thermowatch": "https://karaoke.thermowatch.com.br",
        },
    }
    return jsonify(out)


@network_maestro_standalone_bp.route("/api/wifi/connect", methods=["POST"])
def wifi_connect():
    """Connect Dell Wi-Fi and sync the operator peer to the same network when available."""
    payload = request.get_json(silent=True) or request.form
    ssid = str(payload.get("ssid", "")).strip()
    password = str(payload.get("password", "")).strip()

    try:
        dell_result = connect_wifi_network(ssid, password or None)
    except WifiSetupError as exc:
        return jsonify({"success": False, "error": str(exc)}), 400

    try:
        peer_result = connect_dell_wifi_network(ssid, password or None)
    except DellWifiSyncError as exc:
        return (
            jsonify(
                {
                    "success": False,
                    "error": f"{dell_result.get('message', 'Dell conectado.')} Mas o operador remoto nao acompanhou: {exc}",
                    "dell": dell_result,
                }
            ),
            502,
        )

    return jsonify(
        {
            "success": True,
            "message": f"{dell_result.get('message', '')} {peer_result.get('message', '')}".strip(),
            "dell": dell_result,
            "peer": peer_result,
        }
    )


@network_maestro_standalone_bp.route("/api/wifi/disconnect", methods=["POST"])
def wifi_disconnect():
    """Disconnect Dell and operator peer Wi-Fi when available."""
    try:
        dell_result = disconnect_wifi_network()
    except WifiSetupError as exc:
        return jsonify({"success": False, "error": str(exc)}), 400

    try:
        peer_result = disconnect_dell_wifi_network()
    except DellWifiSyncError as exc:
        return (
            jsonify(
                {
                    "success": False,
                    "error": f"{dell_result.get('message', 'Wi-Fi do Dell desconectado.')} Mas o operador remoto nao acompanhou: {exc}",
                    "dell": dell_result,
                }
            ),
            502,
        )

    return jsonify(
        {
            "success": True,
            "message": f"{dell_result.get('message', '')} {peer_result.get('message', '')}".strip(),
            "dell": dell_result,
            "peer": peer_result,
        }
    )
