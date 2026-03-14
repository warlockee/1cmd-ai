#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")" && pwd)"
UNIT_SRC="$ROOT_DIR/systemd/onecmd.service"
UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
UNIT_DST="$UNIT_DIR/onecmd.service"

if [[ ! -f "$ROOT_DIR/.env" ]]; then
  echo "[ERROR] Missing $ROOT_DIR/.env. Run ./setup.sh first."
  exit 1
fi

if [[ ! -x "$ROOT_DIR/.venv/bin/onecmd" ]]; then
  echo "[ERROR] Missing $ROOT_DIR/.venv/bin/onecmd. Run setup/install first."
  exit 1
fi

mkdir -p "$UNIT_DIR"
sed "s#@ROOT_DIR@#$ROOT_DIR#g" "$UNIT_SRC" > "$UNIT_DST"

systemctl --user daemon-reload
systemctl --user enable --now onecmd.service

cat <<EOF
[OK] Installed user service: $UNIT_DST

Useful commands:
  systemctl --user status onecmd.service
  journalctl --user -u onecmd.service -f
  systemctl --user restart onecmd.service
  systemctl --user stop onecmd.service

To auto-start even when not logged in:
  sudo loginctl enable-linger $USER
EOF
