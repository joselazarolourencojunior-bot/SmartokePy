"""Helpers for the Network Maestro panel shown to the Dell operator."""

from __future__ import annotations

import json
import os
import socket
from datetime import datetime, timezone
from urllib.error import URLError
from urllib.request import urlopen

import psutil

from pikaraoke.lib.dell_wifi_sync import get_dell_wifi_status
from pikaraoke.lib.wifi_setup import WifiSetupError, get_wifi_status

_ETHERNET_PREFIXES = ("eth", "en", "eno", "enp", "enx")
_WIFI_PREFIXES = ("wlan", "wl", "wifi", "wi-fi")
_INTERNET_CHECK_TARGETS = (("1.1.1.1", 53), ("8.8.8.8", 53))
_NGROK_API_URL = "http://127.0.0.1:4040/api/tunnels"
_DEFAULT_OPERATOR_HOST = os.environ.get("NETWORK_MAESTRO_OPERATOR_HOST", "192.168.15.9")
_DEFAULT_MAESTRO_PORT = int(os.environ.get("NETWORK_MAESTRO_PORT", "5560"))
_DEFAULT_KARAOKE_PORT = int(os.environ.get("NETWORK_MAESTRO_KARAOKE_PORT", "5555"))
_DEFAULT_PORTAL_PORT = int(os.environ.get("NETWORK_MAESTRO_PORTAL_PORT", "3001"))

_CF_CONFIG_CANDIDATES = (
    "/etc/cloudflared/config.yml",
    "/etc/cloudflared/config.yaml",
    "/opt/Karaoke/cloudflared/config.yml",
    os.path.expanduser("~/.cloudflared/config.yml"),
)
_CF_PUBLIC_HOSTNAMES = (
    "portal.thermowatch.com.br",
    "karaoke.thermowatch.com.br",
)


def _clean_text(value: object) -> str:
    """Normalize text values returned by local tools or env vars."""
    cleaned = str(value or "")
    cleaned = cleaned.replace("\u00a0", " ")
    cleaned = cleaned.replace("`", "")
    return cleaned.strip()


def _is_ipv4_address(address: object) -> bool:
    """Return whether an address object is an IPv4 address."""
    family = getattr(address, "family", None)
    return family == socket.AF_INET


def _is_candidate_ip(ip_address: str) -> bool:
    """Return whether the IP is useful for local diagnostics."""
    return bool(ip_address) and not ip_address.startswith(("127.", "169.254."))


def _find_ipv4(addresses: list[object]) -> str:
    """Return the first useful IPv4 address from an interface list."""
    for address in addresses:
        if _is_ipv4_address(address) and _is_candidate_ip(address.address):
            return str(address.address)
    return ""


def _get_interface_kind(interface_name: str) -> str | None:
    """Classify a NetworkManager-friendly interface type from its name."""
    normalized = interface_name.lower()
    if normalized.startswith(_ETHERNET_PREFIXES):
        return "ethernet"
    if normalized.startswith(_WIFI_PREFIXES):
        return "wifi"
    return None


def _get_interfaces_by_kind(kind: str) -> list[dict[str, object]]:
    """Return normalized network interface information for the requested kind."""
    interface_addresses = psutil.net_if_addrs()
    interface_stats = psutil.net_if_stats()
    interfaces: list[dict[str, object]] = []

    for interface_name, addresses in interface_addresses.items():
        if _get_interface_kind(interface_name) != kind:
            continue

        stats = interface_stats.get(interface_name)
        ipv4_address = _find_ipv4(addresses)
        interfaces.append(
            {
                "name": interface_name,
                "ipv4": ipv4_address,
                "is_up": bool(stats and stats.isup),
                "speed_mbps": int(stats.speed) if stats and stats.speed > 0 else None,
            }
        )

    interfaces.sort(key=lambda item: (not item["is_up"], not bool(item["ipv4"]), item["name"]))
    return interfaces


def _pick_primary_interface(interfaces: list[dict[str, object]]) -> dict[str, object] | None:
    """Pick the best interface to display as the current control channel."""
    for interface in interfaces:
        if interface["is_up"] and interface["ipv4"]:
            return interface
    for interface in interfaces:
        if interface["is_up"]:
            return interface
    return interfaces[0] if interfaces else None


def _check_internet_reachability(timeout_seconds: float = 0.35) -> bool:
    """Probe raw TCP connectivity without depending on DNS."""
    for host, port in _INTERNET_CHECK_TARGETS:
        try:
            with socket.create_connection((host, port), timeout=timeout_seconds):
                return True
        except OSError:
            continue
    return False


def _join_url(base_url: str, path: str) -> str:
    """Join a base URL with a leading-slash path."""
    base_url = _clean_text(base_url)
    if not path:
        return base_url.rstrip("/")
    return f"{base_url.rstrip('/')}{path if path.startswith('/') else f'/{path}'}"


def _get_ngrok_status(timeout_seconds: float = 0.35) -> dict[str, object]:
    """Return the current public tunnel status from the local ngrok API."""
    try:
        with urlopen(_NGROK_API_URL, timeout=timeout_seconds) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (URLError, TimeoutError, OSError, json.JSONDecodeError, ValueError):
        return {
            "online": False,
            "public_url": "",
            "message": "Ngrok indisponivel neste momento.",
            "target": "",
        }

    tunnels = payload.get("tunnels") or []
    if not isinstance(tunnels, list) or not tunnels:
        return {
            "online": False,
            "public_url": "",
            "message": "Ngrok ativo sem tunel publico no momento.",
            "target": "",
        }

    http_tunnel = None
    for tunnel in tunnels:
        if isinstance(tunnel, dict) and str(tunnel.get("public_url", "")).startswith("https://"):
            http_tunnel = tunnel
            break

    if http_tunnel is None:
        http_tunnel = tunnels[0] if isinstance(tunnels[0], dict) else {}

    public_url = _clean_text(http_tunnel.get("public_url", ""))
    config = http_tunnel.get("config") if isinstance(http_tunnel.get("config"), dict) else {}
    address = _clean_text(config.get("addr", ""))

    return {
        "online": bool(public_url),
        "public_url": public_url,
        "message": (
            f"Link publico ativo em {public_url}."
            if public_url
            else "Ngrok ativo, mas sem URL publica identificada."
        ),
        "target": address,
    }


def _proc_running(cmd_substring: str) -> bool:
    """Return True if any running process contains cmd_substring."""
    try:
        for proc in psutil.process_iter(attrs=["name", "cmdline"]):
            try:
                cmdline = " ".join(proc.info.get("cmdline") or [])
                if cmd_substring in cmdline or cmd_substring in (proc.info.get("name") or ""):
                    return True
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
    except Exception:
        pass
    return False


def _http_ok(url: str, timeout_seconds: float = 1.0, only_head: bool = True) -> tuple[bool, int, str]:
    """Probe a URL and return (ok, status_code, first_bytes_or_error)."""
    import subprocess as _sp
    # Uses subprocess curl to avoid issues with ssl certs/network stack inside systemd
    try:
        cmd = [
            "curl", "-sSL", "-m", str(max(1, int(timeout_seconds))),
            "-o", "/dev/null", "-w", "%{http_code}", url,
        ]
        rc = _sp.run(cmd, capture_output=True, text=True, timeout=int(timeout_seconds) + 2)
        code_raw = (rc.stdout or "").strip()
        if code_raw.isdigit():
            code = int(code_raw)
            return (200 <= code < 500), code, f"HTTP {code}"
        return False, 0, rc.stderr.strip()[:240] or (f"exit {rc.returncode}")
    except Exception as e:
        return False, 0, f"{type(e).__name__}: {e}"[:240]


def _get_cloudflare_tunnels_status(timeout_seconds: float = 1.0) -> dict[str, object]:
    """Detect Cloudflare Tunnel (cloudflared) running, config, and public hostnames reachable."""
    running = _proc_running("cloudflared")
    service_active = False
    config_path = ""
    for p in _CF_CONFIG_CANDIDATES:
        try:
            if os.path.exists(p):
                config_path = p
                break
        except OSError:
            continue

    # Try to detect active hostnames: check known hostnames via HTTPS
    hostnames_up: list[dict[str, str]] = []
    first_public_url = ""
    total_ok = 0
    for host in _CF_PUBLIC_HOSTNAMES:
        url = f"https://{host}/"
        ok, code, note = _http_ok(url, timeout_seconds=timeout_seconds)
        if ok:
            total_ok += 1
            if not first_public_url and "portal" in host:
                first_public_url = url
            hostnames_up.append({"hostname": host, "url": url, "status": str(code)})
        else:
            hostnames_up.append({"hostname": host, "url": url, "status": note})

    online = running and total_ok > 0
    if not first_public_url and hostnames_up:
        first_public_url = hostnames_up[0]["url"]

    message = (
        f"Tunel Cloudflare ativo: {total_ok}/{len(_CF_PUBLIC_HOSTNAMES)} dominios respondendo."
        if online
        else
        ("cloudflared rodando, mas hostnames publicos nao responderam ainda."
         if running
         else "cloudflared nao esta rodando neste momento.")
    )

    return {
        "online": bool(online),
        "running": bool(running),
        "service_active": bool(service_active),
        "config_path": config_path,
        "public_url": first_public_url,
        "message": message,
        "target": "cloudflared-systemd",
        "hostnames": hostnames_up,
        "portal": "https://portal.thermowatch.com.br",
        "karaoke": "https://karaoke.thermowatch.com.br",
    }


def _get_public_tunnels_status(timeout_seconds: float = 1.0) -> dict[str, object]:
    """Unified public tunnel status (Cloudflare primary; ngrok kept as fallback)."""
    cf = _get_cloudflare_tunnels_status(timeout_seconds=timeout_seconds)
    ng = _get_ngrok_status(timeout_seconds=min(0.6, timeout_seconds * 0.6))
    return {
        "online": bool(cf.get("online") or ng.get("online")),
        "provider": "cloudflare" if cf.get("online") else ("ngrok" if ng.get("online") else "nenhum"),
        "cloudflare": cf,
        "ngrok": ng,
        "public_url": _clean_text(cf.get("public_url") or ng.get("public_url") or ""),
        "message": _clean_text(cf.get("message") if cf.get("online") else (ng.get("message") if ng.get("online") else (cf.get("message") or ng.get("message")))),
    }


def _get_local_peer_status(wifi_status: dict[str, object], dell_same_network: bool) -> dict[str, object]:
    """Build operator peer status — on this Dell-only installation, the peer IS the Dell itself.

    Old name 'operator_peer' referenced a secondary Raspberry/Operador device that no longer
    exists. We keep the shape for compatibility with the HTML panel but mark it 'reachable'
    when Dell Wi-Fi is connected.
    """
    dell_connected = bool(wifi_status.get("connected"))
    dell_ssid = _clean_text(wifi_status.get("ssid", ""))
    dell_device = _clean_text(wifi_status.get("device", ""))
    dell_ip = _clean_text(wifi_status.get("ip_address", ""))
    dell_signal = wifi_status.get("signal")
    return {
        "reachable": bool(dell_connected or bool(wifi_status)),
        "message": (
            "Este Dell controla tudo agora (nao ha segundo operador remoto)."
            if dell_connected
            else "Aguardando Wi-Fi do Dell conectar."
        ),
        "wifi_connected": dell_connected,
        "wifi_ssid": dell_ssid,
        "wifi_ip": dell_ip,
        "host": _DEFAULT_OPERATOR_HOST,
        "device": dell_device,
        "signal": dell_signal,
        "same_network_as_raspberry": bool(dell_same_network or dell_connected),
        "same_network_as_operator": bool(dell_same_network or dell_connected),
    }


def _build_operating_mode(
    control_ready: bool,
    wifi_status: dict[str, object],
    internet_available: bool,
    public_tunnels: dict[str, object],
) -> dict[str, object]:
    """Summarize the current operational mode for the operator."""
    wifi_connected = bool(wifi_status.get("connected"))
    public_ready = bool(internet_available and public_tunnels.get("online"))
    provider = str(public_tunnels.get("provider") or "")

    if public_ready:
        prov_label = "Cloudflare" if provider == "cloudflare" else ("Ngrok" if provider == "ngrok" else "Tunel")
        return {
            "code": "clientes_via_internet",
            "title": "Clientes via internet",
            "ready": True,
            "operator_channel": "Wi-Fi do Dell",
            "client_channel": f"Link publico / {prov_label}",
            "message": (
                "O operador continua controlando tudo localmente pelo Dell, "
                "e os clientes devem entrar pelos links publicos."
            ),
            "next_step": (
                "Conferir se os links publicos abertos no Cloudflare (portal e karaoke) "
                "sao os esperados para acesso externo."
            ),
        }

    if internet_available and not public_tunnels.get("online"):
        return {
            "code": "internet_sem_tunel",
            "title": "Internet sem link publico",
            "ready": False,
            "operator_channel": "Wi-Fi do Dell",
            "client_channel": "Aguardando Cloudflare",
            "message": (
                "Ha internet, mas o tunel publico (Cloudflare) ainda nao esta pronto. "
                "O operador segue com controle local pelo Wi-Fi do Dell."
            ),
            "next_step": "Verificar o servico cloudflared.service antes de liberar acesso para clientes.",
        }

    if control_ready and wifi_connected:
        return {
            "code": "somente_operador_local",
            "title": "Somente operador local",
            "ready": True,
            "operator_channel": "Wi-Fi do Dell",
            "client_channel": "Indisponivel agora",
            "message": (
                "O Dell esta conectado ao Wi-Fi, mas nao ha caminho publico confirmado. "
                "O operador ainda consegue reorganizar tudo localmente."
            ),
            "next_step": "Testar internet do local ou hotspot para permitir uso do Cloudflare Tunnel.",
        }

    if control_ready:
        return {
            "code": "controle_local_emergencia",
            "title": "Controle local de emergencia",
            "ready": True,
            "operator_channel": "Wi-Fi do Dell",
            "client_channel": "Indisponivel agora",
            "message": "Sem Wi-Fi util no momento. A rede local do Dell segue como linha de vida para reconectar.",
            "next_step": "Usar o maestro para entrar numa rede com internet.",
        }

    return {
        "code": "sem_caminho_estavel",
        "title": "Sem caminho estavel",
        "ready": False,
        "operator_channel": "A confirmar",
        "client_channel": "Indisponivel",
        "message": "Nem o Wi-Fi do Dell nem o caminho publico estao prontos o suficiente agora.",
        "next_step": "Checar o Wi-Fi do Dell e religar o canal de controle antes de seguir.",
    }


def _build_operational_links(public_tunnels: dict[str, object]) -> dict[str, object]:
    """Build the main local and public URLs used in operation.

    Cloudflare primary: we always publish portal.thermowatch.com.br and karaoke.thermowatch.com.br
    as the canonical internet entries; ngrok remains a fallback only.
    """
    operator_host = _DEFAULT_OPERATOR_HOST
    maestro_base = f"http://{operator_host}:{_DEFAULT_MAESTRO_PORT}"
    karaoke_base = f"http://{operator_host}:{_DEFAULT_KARAOKE_PORT}"
    portal_base = f"http://{operator_host}:{_DEFAULT_PORTAL_PORT}"

    cf = public_tunnels.get("cloudflare") if isinstance(public_tunnels.get("cloudflare"), dict) else {}
    portal_public = _clean_text(cf.get("portal") or "https://portal.thermowatch.com.br")
    karaoke_public = _clean_text(cf.get("karaoke") or "https://karaoke.thermowatch.com.br")
    # fallback: unified public_url (ngrok / primary cloudflare)
    unified_public = _clean_text(public_tunnels.get("public_url") or portal_public)

    local_links = [
        {"label": "Maestro de rede", "url": maestro_base, "description": "Painel do operador no Dell (Wi-Fi local)."},
        {"label": "Assistente Wi-Fi", "url": _join_url(karaoke_base, "/wifi-setup"), "description": "Tela de apoio do SmartokePy para rede."},
        {"label": "Karaoke local", "url": karaoke_base, "description": "Interface principal do SmartokePy no Dell."},
        {"label": "Splash local", "url": _join_url(karaoke_base, "/splash"), "description": "Tela de exibicao da TV."},
        {"label": "Fila local", "url": _join_url(karaoke_base, "/queue"), "description": "Fila local do karaoke."},
        {"label": "Portal local", "url": portal_base, "description": "Portal administrativo do Dell."},
    ]

    public_links: list[dict[str, str]] = []
    if public_tunnels.get("online") or portal_public:
        public_links = [
            {"label": "Portal publico", "url": portal_public, "description": "Entrada do portal pela internet (Cloudflare)."},
            {"label": "Login publico", "url": _join_url(portal_public, "/login"), "description": "Tela de login do portal."},
            {"label": "Painel publico / Operacao", "url": _join_url(portal_public, "/operacao"), "description": "Painel operacional admin pela internet."},
            {"label": "Mesa / entrada clientes", "url": _join_url(portal_public, "/"), "description": "Entrada para clientes informarem codigo da mesa."},
            {"label": "Karaoke publico", "url": karaoke_public, "description": "Interface SmartokePy / fila publica (Cloudflare)."},
        ]
        if unified_public and unified_public.rstrip("/") not in {portal_public.rstrip("/"), karaoke_public.rstrip("/")}:
            public_links.insert(0, {"label": "Link publico unificado", "url": unified_public, "description": "URL primaria detectada do tunel."})

    return {
        "local": local_links,
        "public": public_links,
        "public_active": bool(public_links),
    }


def _links_to_contract_map(links: list[dict[str, str]]) -> dict[str, str]:
    """Convert UI link cards into a stable API map."""
    normalized: dict[str, str] = {}
    for item in links:
        label = _clean_text(item.get("label", "")).lower()
        url = _clean_text(item.get("url", ""))
        if not label or not url:
            continue

        key = (
            label.replace(" ", "_")
            .replace("-", "_")
            .replace("/", "_")
            .replace("{", "")
            .replace("}", "")
        )
        normalized[key] = url
    return normalized


def get_network_maestro_status() -> dict[str, object]:
    """Return a compact live network snapshot for the operator panel."""
    ethernet_interfaces = _get_interfaces_by_kind("ethernet")
    wifi_interfaces = _get_interfaces_by_kind("wifi")
    primary_wifi = _pick_primary_interface(wifi_interfaces)
    primary_ethernet = _pick_primary_interface(ethernet_interfaces)

    try:
        wifi_status = get_wifi_status()
    except WifiSetupError as exc:
        wifi_status = {
            "supported": False,
            "connected": False,
            "ssid": "",
            "signal": None,
            "ip_address": "",
            "connection_name": "",
            "device": None,
            "radio_enabled": False,
            "message": str(exc),
        }

    dell_wifi_status = get_dell_wifi_status()
    same_wifi_network = bool(
        wifi_status.get("connected")
        and dell_wifi_status.get("wifi_connected")
        and _clean_text(wifi_status.get("ssid", ""))
        and _clean_text(wifi_status.get("ssid", "")) == _clean_text(dell_wifi_status.get("wifi_ssid", ""))
    )

    control_ready = bool(wifi_status.get("connected") or (primary_wifi and primary_wifi.get("is_up")))
    internet_available = _check_internet_reachability()
    public_tunnels = _get_public_tunnels_status()
    ngrok_status = public_tunnels.get("ngrok") if isinstance(public_tunnels.get("ngrok"), dict) else {}
    cloudflare_status = public_tunnels.get("cloudflare") if isinstance(public_tunnels.get("cloudflare"), dict) else {}
    public_ready = bool(internet_available and public_tunnels.get("online"))
    operational_links = _build_operational_links(public_tunnels)
    operating_mode = _build_operating_mode(
        control_ready,
        wifi_status,
        internet_available,
        public_tunnels,
    )
    operator_peer = _get_local_peer_status(wifi_status, bool(same_wifi_network or wifi_status.get("connected")))

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "control_channel": {
            "mode": "wifi-local",
            "ready": control_ready,
            "message": (
                "Wi-Fi do Dell pronto para operar."
                if control_ready
                else "Wi-Fi do Dell indisponivel ou sem link no momento."
            ),
            "primary": primary_wifi,
            "interfaces": wifi_interfaces,
            "ethernet": primary_ethernet,
            "ethernet_interfaces": ethernet_interfaces,
        },
        "wifi": wifi_status,
        "operator_peer": operator_peer,
        "internet": {
            "online": internet_available,
            "message": (
                "Internet detectada para uso do Cloudflare Tunnel e servicos online."
                if internet_available
                else "Sem internet agora. O controle local pelo Wi-Fi do Dell continua."
            ),
        },
        "public_access": {
            "ready": public_ready,
            "mode": "internet-publica" if public_ready else "local-operador",
            "message": (
                "Clientes podem usar o caminho publico pela internet (Cloudflare)."
                if public_ready
                else "Clientes ainda nao tem um caminho publico confirmado."
            ),
        },
        "operational_links": operational_links,
        "operating_mode": operating_mode,
        "public_tunnels": public_tunnels,
        "cloudflare": cloudflare_status,
        "ngrok": ngrok_status,
    }


def get_network_maestro_contract() -> dict[str, object]:
    """Return a stable, portal-friendly summary of the maestro state."""
    status = get_network_maestro_status()
    control_channel = status.get("control_channel", {})
    wifi = status.get("wifi", {})
    internet = status.get("internet", {})
    ngrok = status.get("ngrok", {})
    cloudflare = status.get("cloudflare") if isinstance(status.get("cloudflare"), dict) else {}
    public_tunnels = status.get("public_tunnels") if isinstance(status.get("public_tunnels"), dict) else {}
    operating_mode = status.get("operating_mode", {})
    operational_links = status.get("operational_links", {})
    operator_peer = status.get("operator_peer") or {}

    local_links = _links_to_contract_map(list(operational_links.get("local", [])))
    public_links = _links_to_contract_map(list(operational_links.get("public", [])))

    control_primary = control_channel.get("primary") or {}
    ethernet_primary = control_channel.get("ethernet") or {}
    dell_same_network = bool(
        operator_peer.get("same_network_as_operator")
        or operator_peer.get("same_network_as_raspberry")
    )

    return {
        "generated_at": _clean_text(status.get("generated_at")),
        "mode": {
            "code": _clean_text(operating_mode.get("code", "")),
            "title": _clean_text(operating_mode.get("title", "")),
            "ready": bool(operating_mode.get("ready")),
            "operator_channel": _clean_text(operating_mode.get("operator_channel", "")),
            "client_channel": _clean_text(operating_mode.get("client_channel", "")),
        },
        "network": {
            "cable_ready": bool(control_channel.get("ready")),
            "cable_ip": _clean_text(ethernet_primary.get("ipv4", "")),
            "wifi_ready": bool(control_channel.get("ready")),
            "wifi_ip": _clean_text(control_primary.get("ipv4", "")),
            "wifi_connected": bool(wifi.get("connected")),
            "wifi_ssid": _clean_text(wifi.get("ssid", "")),
            "dell_reachable": bool(operator_peer.get("reachable")),
            "dell_wifi_connected": bool(operator_peer.get("wifi_connected")),
            "dell_wifi_ssid": _clean_text(operator_peer.get("wifi_ssid", "")),
            "dell_wifi_ip": _clean_text(operator_peer.get("wifi_ip", "")),
            "dell_same_network": dell_same_network,
            "internet_online": bool(internet.get("online")),
            "ngrok_online": bool(ngrok.get("online")),
            "ngrok_public_url": _clean_text(ngrok.get("public_url", "")),
            "ngrok_target": _clean_text(ngrok.get("target", "")),
            "cloudflare_online": bool(cloudflare.get("online")),
            "cloudflare_running": bool(cloudflare.get("running")),
            "cloudflare_public_url": _clean_text(cloudflare.get("public_url", "")),
            "cloudflare_portal": _clean_text(cloudflare.get("portal", "")),
            "cloudflare_karaoke": _clean_text(cloudflare.get("karaoke", "")),
            "public_tunnels_online": bool(public_tunnels.get("online")),
            "public_tunnels_provider": _clean_text(public_tunnels.get("provider", "")),
        },
        "urls": {
            "local": local_links,
            "public": public_links,
        },
    }
