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

# If local .env, load it
if [ -f .env ]; then
    export $(cat .env | xargs)
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
