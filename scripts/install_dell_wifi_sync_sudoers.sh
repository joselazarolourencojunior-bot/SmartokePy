#!/bin/sh
set -eu

TARGET_FILE="${TARGET_FILE:-/etc/sudoers.d/91-dell-wifi-from-rpi}"
TARGET_USER="${TARGET_USER:-pi}"

if [ "$(id -u)" -ne 0 ]; then
  echo "Execute este script com sudo no Dell."
  exit 1
fi

tee "$TARGET_FILE" >/dev/null <<EOF
$TARGET_USER ALL=(root) NOPASSWD: /usr/bin/nmcli
EOF

chmod 440 "$TARGET_FILE"
visudo -cf "$TARGET_FILE"

echo
echo "Permissao aplicada com sucesso."
echo "Arquivo: $TARGET_FILE"
echo "Usuario liberado: $TARGET_USER"
echo
echo "Teste local no Dell:"
echo "  sudo -n nmcli radio wifi"
