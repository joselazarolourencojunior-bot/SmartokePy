"""Helpers for synchronizing Dell Wi-Fi state from the Raspberry over the cable link."""

from __future__ import annotations

import shlex
import shutil
import subprocess
import os


class DellWifiSyncError(RuntimeError):
    """Raised when the Raspberry cannot orchestrate the Dell Wi-Fi state."""


_DEFAULT_DELL_HOST = os.environ.get("NETWORK_MAESTRO_DELL_HOST", "10.10.10.1").strip()
_DEFAULT_DELL_USER = os.environ.get("NETWORK_MAESTRO_DELL_USER", "pi").strip()
_DEFAULT_DELL_SSH_PORT = os.environ.get("NETWORK_MAESTRO_DELL_SSH_PORT", "22").strip() or "22"
_SYNC_DELL_ENABLED = os.environ.get("NETWORK_MAESTRO_SYNC_DELL", "0").strip().lower() not in {"0", "false", "no", "off"}


def _split_nmcli_line(line: str) -> list[str]:
    """Split terse nmcli output while preserving ':' inside the SSID."""
    parts = line.split(":")
    if len(parts) <= 4:
        return parts

    return [parts[0], ":".join(parts[1:-2]), parts[-2], parts[-1]]


def is_dell_wifi_sync_supported() -> bool:
    """Return whether this Raspberry is configured to orchestrate the Dell over SSH."""
    return bool(_SYNC_DELL_ENABLED and _DEFAULT_DELL_HOST and _DEFAULT_DELL_USER and shutil.which("ssh"))


def _friendly_ssh_error(error_text: str) -> str:
    """Convert raw SSH or sudo errors into short operator-facing messages."""
    lowered = error_text.lower()

    if "host key verification failed" in lowered:
        return "Falta confiar a chave SSH do Dell a partir do Raspberry."
    if "permission denied" in lowered:
        return "O Raspberry ainda nao consegue entrar por SSH no Dell sem senha."
    if "could not resolve hostname" in lowered or "no route to host" in lowered:
        return "O Dell nao foi alcancado pelo cabo em 10.10.10.1."
    if "connection timed out" in lowered or "operation timed out" in lowered:
        return "O Raspberry demorou demais para falar com o Dell pelo cabo."
    if "sudo:" in lowered or "a password is required" in lowered:
        return "Falta liberar o nmcli sem senha no Dell para o usuario pi."
    if "not authorized" in lowered:
        return "O Dell recusou a troca de Wi-Fi por falta de permissao."
    if "nmcli" in lowered and "not found" in lowered:
        return "O Dell nao tem o NetworkManager (nmcli) disponivel."
    if error_text.strip():
        return error_text.strip()
    return "Nao foi possivel sincronizar o Wi-Fi do Dell."


def _run_dell_ssh(remote_command: str, timeout: int = 20) -> str:
    """Run a shell command on the Dell through the point-to-point cable link."""
    if not is_dell_wifi_sync_supported():
        raise DellWifiSyncError("Sincronismo com o Dell desativado ou sem SSH disponivel neste Raspberry.")

    command = [
        "ssh",
        "-p",
        _DEFAULT_DELL_SSH_PORT,
        "-o",
        "BatchMode=yes",
        "-o",
        "StrictHostKeyChecking=accept-new",
        "-o",
        "ConnectTimeout=5",
        f"{_DEFAULT_DELL_USER}@{_DEFAULT_DELL_HOST}",
        "sh",
        "-lc",
        remote_command,
    ]

    try:
        result = subprocess.run(
            command,
            check=True,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise DellWifiSyncError("O Dell nao respondeu a tempo pelo cabo.") from exc
    except subprocess.CalledProcessError as exc:
        error_text = (exc.stderr or exc.stdout or "").strip()
        raise DellWifiSyncError(_friendly_ssh_error(error_text)) from exc

    return result.stdout.strip()


def get_dell_wifi_status() -> dict[str, object]:
    """Return the current Dell Wi-Fi status seen from the Raspberry."""
    status: dict[str, object] = {
        "sync_enabled": bool(_SYNC_DELL_ENABLED),
        "supported": is_dell_wifi_sync_supported(),
        "reachable": False,
        "host": _DEFAULT_DELL_HOST,
        "user": _DEFAULT_DELL_USER,
        "device": "",
        "wifi_connected": False,
        "wifi_ssid": "",
        "wifi_signal": None,
        "wifi_ip": "",
        "radio_enabled": False,
        "message": "",
    }

    if not _SYNC_DELL_ENABLED:
        status["message"] = "Sincronismo com o Dell desativado por configuracao."
        return status

    if not status["supported"]:
        status["message"] = "SSH indisponivel no Raspberry para controlar o Dell."
        return status

    try:
        output = _run_dell_ssh(
            r'''
DEV="$(sudo -n nmcli -t -f DEVICE,TYPE device status | awk -F: '$2=="wifi"{print $1; exit}')"
printf 'DEVICE=%s\n' "$DEV"
printf 'HOSTNAME=%s\n' "$(hostname 2>/dev/null || echo dell)"
printf 'RADIO='
sudo -n nmcli radio wifi
if [ -n "$DEV" ]; then
  sudo -n nmcli -t -f GENERAL.STATE,GENERAL.CONNECTION,IP4.ADDRESS device show "$DEV"
  sudo -n nmcli -t -f IN-USE,SSID,SIGNAL,SECURITY device wifi list ifname "$DEV" --rescan no
fi
''',
            timeout=20,
        )
    except DellWifiSyncError as exc:
        status["message"] = str(exc)
        return status

    status["reachable"] = True
    status["message"] = "Dell alcançado pelo cabo."

    for line in output.splitlines():
        if line.startswith("DEVICE="):
            status["device"] = line.split("=", 1)[1].strip()
        elif line.startswith("HOSTNAME="):
            status["hostname"] = line.split("=", 1)[1].strip()
        elif line.startswith("RADIO="):
            status["radio_enabled"] = "enabled" in line.lower()
        elif line.startswith("GENERAL.STATE:"):
            status["wifi_connected"] = "connected" in line.lower()
        elif line.startswith("GENERAL.CONNECTION:"):
            connection_name = line.split(":", 1)[1].strip()
            if connection_name and connection_name != "--":
                status["connection_name"] = connection_name
        elif line.startswith("IP4.ADDRESS"):
            value = line.split(":", 1)[1].strip()
            if value and not status["wifi_ip"]:
                status["wifi_ip"] = value.split("/", 1)[0]
        elif line.startswith("*:"):
            parts = _split_nmcli_line(line)
            if len(parts) >= 4:
                _, ssid, signal, _security = parts[:4]
                status["wifi_connected"] = True
                status["wifi_ssid"] = ssid.strip()
                status["wifi_signal"] = int(signal or 0)

    if status["wifi_connected"] and not status["wifi_ssid"]:
        status["wifi_ssid"] = str(status.get("connection_name") or "")

    if status["wifi_connected"] and status["wifi_ssid"]:
        status["message"] = f"Dell conectado em {status['wifi_ssid']}."
    elif status["reachable"]:
        status["message"] = "Dell alcançado, mas sem Wi-Fi conectado."

    return status


def connect_dell_wifi_network(ssid: str, password: str | None = None) -> dict[str, str]:
    """Connect the Dell to the given Wi-Fi network through the cable link."""
    ssid = ssid.strip()
    if not ssid:
        raise DellWifiSyncError("Informe o nome da rede para sincronizar o Dell.")

    quoted_ssid = shlex.quote(ssid)
    password_arg = ""
    if password:
        password_arg = f" password {shlex.quote(password)}"

    _run_dell_ssh(
        f"""
DEV="$(sudo -n nmcli -t -f DEVICE,TYPE device status | awk -F: '$2=="wifi"{{print $1; exit}}')"
[ -n "$DEV" ] || {{ echo "Sem interface Wi-Fi no Dell." >&2; exit 10; }}
sudo -n nmcli radio wifi on
CURRENT="$(sudo -n nmcli -t -f GENERAL.CONNECTION device show "$DEV" | sed -n 's/^GENERAL.CONNECTION://p' | head -n1)"
if [ -n "$CURRENT" ] && [ "$CURRENT" != "--" ] && [ "$CURRENT" != {quoted_ssid} ]; then
  sudo -n nmcli device disconnect "$DEV" >/dev/null 2>&1 || true
fi
sudo -n nmcli -w 45 connection up {quoted_ssid} >/dev/null 2>&1 || sudo -n nmcli -w 60 device wifi connect {quoted_ssid} ifname "$DEV"{password_arg}
""",
        timeout=75,
    )

    status = get_dell_wifi_status()
    return {
        "message": f"Dell sincronizado em {status.get('wifi_ssid') or ssid}.",
        "ssid": str(status.get("wifi_ssid") or ssid),
        "ip_address": str(status.get("wifi_ip") or ""),
    }


def disconnect_dell_wifi_network() -> dict[str, str]:
    """Disconnect the current Dell Wi-Fi network through the cable link."""
    _run_dell_ssh(
        r'''
DEV="$(sudo -n nmcli -t -f DEVICE,TYPE device status | awk -F: '$2=="wifi"{print $1; exit}')"
[ -n "$DEV" ] || { echo "Sem interface Wi-Fi no Dell." >&2; exit 10; }
sudo -n nmcli -w 45 device disconnect "$DEV"
''',
        timeout=45,
    )

    return {
        "message": "Dell desconectado do Wi-Fi e mantido no cabo ponto a ponto.",
    }
