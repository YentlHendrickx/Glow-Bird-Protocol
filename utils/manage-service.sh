#!/bin/bash
param=$1
if [ -z "$param" ]; then
    echo "Usage: $0 {start|stop|status|restart|status_watch}"
    exit 1
fi

if [[ ! "$param" =~ ^(start|stop|status|restart|status_watch)$ ]]; then
    echo "Error: Invalid parameter '$param'."
    echo "Usage: $0 {start|stop|status|restart|status_watch}"
    exit 1
fi

key="GLOWBIRD_SERVICE_NAME"

# Load .env from the repo root (one level up from utils/), regardless of CWD.
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
if [ -f "$ROOT/.env" ]; then
    export $(grep -v '^#' "$ROOT/.env" | xargs)
fi

if [ -z "${!key}" ]; then
    echo "Error: Environment variable $key is not set."
    exit 1
fi

if [ "$param" == "start" ]; then
    systemctl --user start "${!key}".service
elif [ "$param" == "stop" ]; then
    systemctl --user stop "${!key}".service
elif [ "$param" == "status" ]; then
    systemctl --user status "${!key}".service
elif [ "$param" == "restart" ]; then
    systemctl --user restart "${!key}".service
elif [ "$param" == "status_watch" ]; then
    watch -c SYSTEMD_COLORS=1 systemctl --user status "${!key}".service
fi
