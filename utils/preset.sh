#!/usr/bin/env bash
# Manage conf.txt presets for the WLED audio sync service.
#   utils/preset.sh list            list saved presets
#   utils/preset.sh save <name>     copy the current conf.txt into presets/<name>.conf
#   utils/preset.sh use <name>      point conf.txt at presets/<name>.conf and restart the service
set -euo pipefail

# Repo root is one level up from utils/; conf.txt + presets live in src/.
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SRC="$ROOT/src"
PRESETS="$SRC/presets"
CONF="$SRC/conf.txt"

# Service name from .env (shared with manage-service.sh), with a sensible default.
[ -f "$ROOT/.env" ] && export $(grep -v '^#' "$ROOT/.env" | xargs)
SERVICE="${GLOWBIRD_SERVICE_NAME:-glowbird-protocol}.service"

mkdir -p "$PRESETS"

usage() { grep '^#   ' "$0" | sed 's/^#   //'; exit "${1:-0}"; }

restart() {
  if systemctl --user is-active --quiet "$SERVICE" 2>/dev/null; then
    systemctl --user restart "$SERVICE" && echo "restarted $SERVICE (user)"
  elif systemctl is-active --quiet "$SERVICE" 2>/dev/null; then
    sudo systemctl restart "$SERVICE" && echo "restarted $SERVICE (system)"
  else
    echo "note: $SERVICE not running - start it to apply."
  fi
}

case "${1:-list}" in
  list|ls)
    ls -1 "$PRESETS"/*.conf 2>/dev/null | xargs -rn1 basename | sed 's/\.conf$//' || true
    ;;
  save)
    name="${2:?usage: preset.sh save <name>}"
    cp -L "$CONF" "$PRESETS/$name.conf"
    echo "saved $PRESETS/$name.conf"
    ;;
  use|load)
    name="${2:?usage: preset.sh use <name>}"
    [ -f "$PRESETS/$name.conf" ] || { echo "no such preset: $name" >&2; exit 1; }
    ln -sfn "presets/$name.conf" "$CONF"   # relative link, resolved inside src/
    echo "conf.txt -> presets/$name.conf"
    restart
    ;;
  *) usage 1 ;;
esac
