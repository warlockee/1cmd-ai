#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

# Load environment variables
set -a
source .env
set +a

# Kill existing onecmd process
pkill -f '.venv/bin/onecmd' 2>/dev/null || true
sleep 1

# Start with nohup so it survives shell exit
nohup .venv/bin/onecmd --admin-port 8088 --use-weak-security >> /tmp/onecmd.log 2>&1 &
disown
echo "onecmd restarted (pid $!) — log at /tmp/onecmd.log"
