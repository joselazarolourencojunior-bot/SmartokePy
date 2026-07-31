# Maestro de Rede

O Maestro de Rede agora pode rodar como um servico separado do SmartokePy.

## Objetivo

- manter o painel de rede vivo mesmo se o karaoke principal cair
- monitorar cabo ponto a ponto, Wi-Fi do Raspberry e internet
- comandar a troca de Wi-Fi sem depender da interface principal do karaoke

## App standalone

Ponto de entrada:

`pikaraoke.network_maestro_app`

Porta padrao:

`5560`

Healthcheck:

`/health`

API principal:

- `/api/status`
- `/api/contract`
- `/api/wifi/networks`
- `/api/wifi/connect`
- `/api/wifi/disconnect`

## Subir manualmente

```bash
python3 -m pikaraoke.network_maestro_app
```

## Subir como systemd

Arquivo de exemplo:

`network-maestro.service.example`

Instalador automatizado para Raspberry:

`scripts/install_network_maestro_rpi.sh`

Destino sugerido:

`/etc/systemd/system/network-maestro.service`

Depois:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now network-maestro.service
systemctl status network-maestro.service --no-pager -l | head -n 20
curl http://127.0.0.1:5560/health
```

Ou em um passo so a partir do checkout local:

```bash
chmod +x scripts/install_network_maestro_rpi.sh
./scripts/install_network_maestro_rpi.sh
```

## Atalho do Dell

Modelo de arquivo:

`network-maestro-dell.desktop.example`

Linha principal do atalho:

```ini
Exec=sh -c 'xdg-open "$0://$1:5560"' http 10.10.10.2
```

## Observacao

O maestro standalone usa os mesmos helpers de Wi-Fi do Raspberry, mas nao depende do `app.py` principal, nem de `SocketIO`, nem do template `base.html` do SmartokePy.

## Contrato para o portal

Endpoint enxuto para consumo futuro do portal:

`/api/contract`

Campos principais:

- `mode.code`
- `mode.ready`
- `network.cable_ready`
- `network.wifi_connected`
- `network.internet_online`
- `network.ngrok_online`
- `network.ngrok_public_url`
- `urls.local`
- `urls.public`
