param(
    [string]$PiHost = "pi@192.168.15.5",
    [string]$Domain = "ideology-groove-emphasis.ngrok-free.dev"
)

$ErrorActionPreference = "Stop"

$ProjectRoot = Split-Path -Parent $PSScriptRoot
$LocalTemplate = Join-Path $ProjectRoot "templates\index.html"

if (-not (Test-Path $LocalTemplate)) {
    throw "Template nao encontrado em: $LocalTemplate"
}

Write-Host "[1/3] Enviando index.html corrigido para o Raspberry..."
scp "$LocalTemplate" "${PiHost}:~/smartokepy-index.html"

$remoteScript = @'
set -euo pipefail

DOMAIN="DOMAIN_PLACEHOLDER"
TMP_TEMPLATE="$HOME/smartokepy-index.html"

echo "[RPI] Descobrindo caminho real do template instalado..."
TARGET_TEMPLATE="$(python3 -c 'import pathlib, pikaraoke; print(pathlib.Path(pikaraoke.__file__).resolve().parent / "templates" / "index.html")')"
echo "[RPI] Template destino: $TARGET_TEMPLATE"

if install -m 0644 "$TMP_TEMPLATE" "$TARGET_TEMPLATE" 2>/dev/null; then
  echo "[RPI] Template atualizado sem sudo."
else
  sudo install -m 0644 "$TMP_TEMPLATE" "$TARGET_TEMPLATE"
  echo "[RPI] Template atualizado com sudo."
fi

echo "[RPI] Reiniciando SmartokePy..."
sudo systemctl restart smartokepy
sudo systemctl --no-pager --full status smartokepy | sed -n '1,20p'

echo "[RPI] Preparando servico systemd do ngrok..."
NGROK_BIN="$(readlink -f "$(command -v ngrok)")"
if [ -z "$NGROK_BIN" ]; then
  echo "ngrok nao encontrado no PATH." >&2
  exit 1
fi

sudo systemctl disable --now ngrok-karaoke-guard.service karaoke-internet.service >/dev/null 2>&1 || true
pkill -f "ngrok http 5555 --url $DOMAIN" >/dev/null 2>&1 || true

sudo tee /etc/systemd/system/ngrok-karaoke.service >/dev/null <<EOF
[Unit]
Description=ngrok tunnel for SmartokePy karaoke (5555)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=pi
Environment=HOME=/home/pi
ExecStart=$NGROK_BIN http 5555 --url $DOMAIN
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

echo "[RPI] Ativando servico ngrok-karaoke.service..."
sudo systemctl daemon-reload
sudo systemctl enable --now ngrok-karaoke.service
sleep 3
sudo systemctl --no-pager --full status ngrok-karaoke.service | sed -n '1,20p'

echo "[RPI] Tunnels ativos:"
curl -s http://127.0.0.1:4040/api/tunnels
'@

$remoteScript = $remoteScript.Replace("DOMAIN_PLACEHOLDER", $Domain)

Write-Host "[2/3] Aplicando ajuste remoto e ativando ngrok via systemd..."
$remoteScript | ssh -tt $PiHost "bash -s"

Write-Host "[3/3] Concluido."
Write-Host "Teste publico: https://$Domain/"
