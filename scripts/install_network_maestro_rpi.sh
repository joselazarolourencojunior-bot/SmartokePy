#!/bin/sh
set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
PROJECT_DIR=$(CDPATH= cd -- "$SCRIPT_DIR/.." && pwd)

PYTHON_BIN="${PYTHON_BIN:-python3}"
PYTHON_PATH=$(command -v "$PYTHON_BIN" || true)
SERVICE_NAME="${SERVICE_NAME:-network-maestro.service}"
MAESTRO_PORT="${NETWORK_MAESTRO_PORT:-5560}"
MAESTRO_HOST="${NETWORK_MAESTRO_HOST:-0.0.0.0}"
SERVICE_USER="${SERVICE_USER:-pi}"

if [ -z "$PYTHON_PATH" ]; then
  echo "Nao encontrei o Python solicitado: $PYTHON_BIN"
  exit 1
fi

PACKAGE_DIR=$("$PYTHON_BIN" - <<'PY'
import inspect
import os
import pikaraoke

print(os.path.dirname(inspect.getfile(pikaraoke)))
PY
)

if [ ! -d "$PACKAGE_DIR" ]; then
  echo "Nao encontrei o pacote pikaraoke instalado."
  exit 1
fi

copy_file() {
  SOURCE_PATH="$1"
  TARGET_PATH="$2"

  if [ ! -f "$SOURCE_PATH" ]; then
    echo "Arquivo obrigatorio ausente: $SOURCE_PATH"
    exit 1
  fi

  sudo install -D -m 644 "$SOURCE_PATH" "$TARGET_PATH"
}

echo "Projeto local: $PROJECT_DIR"
echo "Pacote instalado: $PACKAGE_DIR"

copy_file "$PROJECT_DIR/lib/network_maestro.py" "$PACKAGE_DIR/lib/network_maestro.py"
copy_file "$PROJECT_DIR/routes/network_maestro.py" "$PACKAGE_DIR/routes/network_maestro.py"
copy_file "$PROJECT_DIR/routes/network_maestro_standalone.py" "$PACKAGE_DIR/routes/network_maestro_standalone.py"
copy_file "$PROJECT_DIR/templates/network_maestro.html" "$PACKAGE_DIR/templates/network_maestro.html"
copy_file "$PROJECT_DIR/templates/network_maestro_standalone.html" "$PACKAGE_DIR/templates/network_maestro_standalone.html"
copy_file "$PROJECT_DIR/network_maestro_app.py" "$PACKAGE_DIR/network_maestro_app.py"

echo "Compilando modulos novos..."
"$PYTHON_BIN" -m py_compile \
  "$PACKAGE_DIR/lib/network_maestro.py" \
  "$PACKAGE_DIR/routes/network_maestro.py" \
  "$PACKAGE_DIR/routes/network_maestro_standalone.py" \
  "$PACKAGE_DIR/network_maestro_app.py"

echo "Instalando servico separado do maestro..."
sudo tee "/etc/systemd/system/$SERVICE_NAME" >/dev/null <<EOF
[Unit]
Description=Maestro de Rede do SmartokePy
After=network.target

[Service]
Type=simple
User=$SERVICE_USER
Environment=HOME=/home/$SERVICE_USER
Environment=NETWORK_MAESTRO_HOST=$MAESTRO_HOST
Environment=NETWORK_MAESTRO_PORT=$MAESTRO_PORT
ExecStart=$PYTHON_PATH -m pikaraoke.network_maestro_app
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable --now "$SERVICE_NAME"

echo
echo "Servico instalado. Resumo:"
systemctl status "$SERVICE_NAME" --no-pager -l | head -n 20
echo
echo "Healthcheck local:"
curl -fsS "http://127.0.0.1:$MAESTRO_PORT/health" || true
echo
echo "Acesso pelo cabo:"
echo "http://10.10.10.2:$MAESTRO_PORT"
