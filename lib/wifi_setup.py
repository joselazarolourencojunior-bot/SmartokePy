"""Helpers for managing Raspberry Pi Wi-Fi connections via NetworkManager."""

from __future__ import annotations

import shutil
import subprocess
import os

from pikaraoke.lib.get_platform import is_linux, is_raspberry_pi


class WifiSetupError(RuntimeError):
    """Raised when Wi-Fi setup actions cannot be completed."""


def _run_nmcli(args: list[str], timeout: int = 30) -> str:
    """Run nmcli and return stdout or raise a user-facing error."""
    if shutil.which("nmcli") is None:
        raise WifiSetupError("O NetworkManager (nmcli) nao esta instalado neste sistema.")

    command = ["nmcli", *args]
    if os.geteuid() != 0 and shutil.which("sudo") is not None:
        command = ["sudo", "-n", *command]

    try:
        result = subprocess.run(
            command,
            check=True,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise WifiSetupError("A operacao de Wi-Fi demorou demais para responder.") from exc
    except subprocess.CalledProcessError as exc:
        error_text = (exc.stderr or exc.stdout or "").strip()
        raise WifiSetupError(_friendly_nmcli_error(error_text)) from exc

    return result.stdout.strip()


def _friendly_nmcli_error(error_text: str) -> str:
    """Convert raw nmcli errors into short messages for non-technical users."""
    error_text = error_text.strip()
    lowered = error_text.lower()

    if "secrets were required" in lowered or "password" in lowered:
        return "Essa rede exige senha. Confira a senha digitada e tente novamente."
    if "no network with ssid" in lowered:
        return "Nao encontrei essa rede Wi-Fi agora. Atualize a lista e tente novamente."
    if "not authorized" in lowered or "permission denied" in lowered:
        return "Sem permissao para alterar o Wi-Fi neste sistema."
    if "a password is required" in lowered or "sudo:" in lowered:
        return "Falta liberar a permissao do nmcli no sistema para o assistente de Wi-Fi."
    if "networkmanager is not running" in lowered:
        return "O servico de rede nao esta ativo no Raspberry no momento."
    if "device not found" in lowered:
        return "Nao encontrei a interface Wi-Fi do Raspberry."
    if error_text:
        return error_text
    return "Nao foi possivel concluir a configuracao do Wi-Fi."


def _split_nmcli_line(line: str) -> list[str]:
    """Split a terse nmcli line while preserving ':' inside the SSID."""
    parts = line.split(":")
    if len(parts) <= 4:
        return parts

    return [parts[0], ":".join(parts[1:-2]), parts[-2], parts[-1]]


def is_wifi_setup_supported() -> bool:
    """Return whether this host can manage Wi-Fi through nmcli."""
    return is_linux() and is_raspberry_pi() and shutil.which("nmcli") is not None


def _set_wifi_radio(enabled: bool) -> None:
    """Turn the Wi-Fi radio on or off."""
    _run_nmcli(["radio", "wifi", "on" if enabled else "off"])


def get_wifi_device() -> str | None:
    """Return the first Wi-Fi device managed by NetworkManager."""
    if not is_wifi_setup_supported():
        return None

    output = _run_nmcli(["-t", "-f", "DEVICE,TYPE", "device", "status"])
    for line in output.splitlines():
        if not line:
            continue
        device, dev_type = (line.split(":", 1) + [""])[:2]
        if dev_type == "wifi":
            return device
    return None


def get_wifi_status() -> dict[str, object]:
    """Return current Wi-Fi state for the Raspberry Pi."""
    supported = is_wifi_setup_supported()
    device = get_wifi_device() if supported else None

    status: dict[str, object] = {
        "supported": supported,
        "device": device,
        "connected": False,
        "ssid": "",
        "signal": None,
        "ip_address": "",
        "connection_name": "",
        "radio_enabled": False,
    }

    if not supported:
        status["message"] = "Disponivel apenas no Raspberry com NetworkManager."
        return status

    if not device:
        status["message"] = "Nenhuma interface Wi-Fi foi encontrada."
        return status

    detail_output = _run_nmcli(
        ["-t", "-f", "GENERAL.STATE,GENERAL.CONNECTION,IP4.ADDRESS", "device", "show", device]
    )
    for line in detail_output.splitlines():
        if line.startswith("GENERAL.STATE:"):
            status["connected"] = "connected" in line.lower()
        elif line.startswith("GENERAL.CONNECTION:"):
            status["connection_name"] = line.split(":", 1)[1].strip()
        elif line.startswith("IP4.ADDRESS"):
            value = line.split(":", 1)[1].strip()
            if value and not status["ip_address"]:
                status["ip_address"] = value.split("/", 1)[0]

    radio_output = _run_nmcli(["radio", "wifi"])
    status["radio_enabled"] = "enabled" in radio_output.lower()

    for network in scan_wifi_networks(rescan=False):
        if network["in_use"]:
            status["ssid"] = network["ssid"]
            status["signal"] = network["signal"]
            status["connected"] = True
            break

    return status


def _list_saved_wifi_profiles() -> set[str]:
    """Return saved Wi-Fi connection names from NetworkManager."""
    output = _run_nmcli(["-t", "-f", "NAME,TYPE", "connection", "show"])
    saved_profiles: set[str] = set()
    for line in output.splitlines():
        if not line:
            continue
        name, conn_type = (line.split(":", 1) + [""])[:2]
        if conn_type == "802-11-wireless" and name.strip():
            saved_profiles.add(name.strip())
    return saved_profiles


def scan_wifi_networks(rescan: bool = True) -> list[dict[str, object]]:
    """Return nearby Wi-Fi networks ordered by quality."""
    device = get_wifi_device()
    if not device:
        raise WifiSetupError("Nao encontrei a interface Wi-Fi do Raspberry.")

    _set_wifi_radio(True)

    command = [
        "-t",
        "-f",
        "IN-USE,SSID,SIGNAL,SECURITY",
        "device",
        "wifi",
        "list",
        "ifname",
        device,
        "--rescan",
        "yes" if rescan else "no",
    ]
    output = _run_nmcli(command, timeout=45)

    saved_profiles = _list_saved_wifi_profiles()
    deduped: dict[str, dict[str, object]] = {}
    for raw_line in output.splitlines():
        if not raw_line:
            continue

        parts = _split_nmcli_line(raw_line)
        if len(parts) < 4:
            continue

        in_use, ssid, signal, security = parts
        ssid = ssid.strip()
        if not ssid:
            continue

        network = {
            "ssid": ssid,
            "signal": int(signal or 0),
            "security": security or "Aberta",
            "in_use": in_use.strip() == "*",
            "saved": ssid in saved_profiles,
        }
        existing = deduped.get(ssid)
        if existing is None or network["signal"] > existing["signal"]:
            deduped[ssid] = network

    return sorted(
        deduped.values(),
        key=lambda item: (not item["in_use"], -int(item["signal"]), str(item["ssid"]).lower()),
    )


def _get_network_security(ssid: str) -> str:
    """Return the reported security mode for a scanned SSID."""
    for network in scan_wifi_networks(rescan=False):
        if str(network.get("ssid", "")).strip() == ssid:
            return str(network.get("security", "") or "")
    return ""


def _is_open_security(security: str) -> bool:
    """Return whether a scanned security label indicates an open network."""
    normalized = security.strip().lower()
    return normalized in {"", "--", "aberta", "open"}


def _delete_existing_wifi_profiles(ssid: str) -> None:
    """Delete stale Wi-Fi profiles for the same SSID before retrying."""
    output = _run_nmcli(["-t", "-f", "NAME,TYPE", "connection", "show"])
    for line in output.splitlines():
        if not line:
            continue
        name, conn_type = (line.split(":", 1) + [""])[:2]
        if conn_type == "802-11-wireless" and name.strip() == ssid:
            _run_nmcli(["connection", "delete", name.strip()], timeout=45)


def _disconnect_if_switching(device: str, target_ssid: str) -> None:
    """Disconnect current Wi-Fi if switching to a different SSID."""
    status = get_wifi_status()
    current_ssid = str(status.get("ssid") or "").strip()
    if status.get("connected") and current_ssid and current_ssid != target_ssid:
        _run_nmcli(["device", "disconnect", device], timeout=45)


def _bring_up_saved_profile(ssid: str) -> bool:
    """Try to reuse a saved Wi-Fi profile for the given SSID."""
    if ssid not in _list_saved_wifi_profiles():
        return False
    _run_nmcli(["connection", "up", ssid], timeout=60)
    return True


def _connect_with_profile(device: str, ssid: str, password: str | None, security: str) -> None:
    """Create a fresh NetworkManager profile when direct connect is not enough."""
    profile_name = ssid
    _delete_existing_wifi_profiles(profile_name)

    _run_nmcli(
        ["connection", "add", "type", "wifi", "ifname", device, "con-name", profile_name, "ssid", ssid],
        timeout=45,
    )

    if not _is_open_security(security):
        if not password:
            raise WifiSetupError("Essa rede exige senha. Digite a senha do Wi-Fi e tente novamente.")
        _run_nmcli(
            [
                "connection",
                "modify",
                profile_name,
                "wifi-sec.key-mgmt",
                "wpa-psk",
                "wifi-sec.psk",
                password,
            ],
            timeout=45,
        )

    _run_nmcli(["connection", "up", profile_name], timeout=60)


def connect_wifi_network(ssid: str, password: str | None = None) -> dict[str, str]:
    """Connect the Raspberry Pi to the given Wi-Fi network."""
    device = get_wifi_device()
    if not device:
        raise WifiSetupError("Nao encontrei a interface Wi-Fi do Raspberry.")

    ssid = ssid.strip()
    if not ssid:
        raise WifiSetupError("Informe o nome da rede Wi-Fi.")

    _set_wifi_radio(True)
    _disconnect_if_switching(device, ssid)

    security = _get_network_security(ssid)
    if not _is_open_security(security) and not password:
        if _bring_up_saved_profile(ssid):
            status = get_wifi_status()
            return {
                "message": f"Raspberry reconectado usando a senha salva da rede {ssid}.",
                "ssid": str(status.get("ssid") or ssid),
                "ip_address": str(status.get("ip_address") or ""),
            }
        raise WifiSetupError("Essa rede exige senha. Digite a senha do Wi-Fi e tente novamente.")

    if not _is_open_security(security):
        _connect_with_profile(device, ssid, password, security)
        status = get_wifi_status()
        return {
            "message": f"Raspberry conectado ou em processo de conexao com a rede {ssid}.",
            "ssid": str(status.get("ssid") or ssid),
            "ip_address": str(status.get("ip_address") or ""),
        }

    command = ["device", "wifi", "connect", ssid, "ifname", device]
    if password:
        command.extend(["password", password])

    try:
        _run_nmcli(command, timeout=60)
    except WifiSetupError as exc:
        error_text = str(exc).lower()
        if "wireless-security.key-mgmt" in error_text:
            _connect_with_profile(device, ssid, password, security)
        else:
            raise

    status = get_wifi_status()
    return {
        "message": f"Raspberry conectado ou em processo de conexao com a rede {ssid}.",
        "ssid": str(status.get("ssid") or ssid),
        "ip_address": str(status.get("ip_address") or ""),
    }


def disconnect_wifi_network() -> dict[str, str]:
    """Disconnect the current Wi-Fi network while keeping the cable path untouched."""
    device = get_wifi_device()
    if not device:
        raise WifiSetupError("Nao encontrei a interface Wi-Fi do Raspberry.")

    _run_nmcli(["device", "disconnect", device], timeout=45)
    return {
        "message": "Wi-Fi desconectado. O acesso pelo cabo continua disponivel.",
    }
