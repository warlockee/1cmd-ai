#!/bin/bash
cd "$(dirname "$0")"

PIDFILE=".onecmd.pid"

echo "Stopping onecmd..."
# Kill by PID file first
if [ -f "$PIDFILE" ]; then
    pid=$(cat "$PIDFILE")
    kill "$pid" 2>/dev/null
    sleep 1
    kill -9 "$pid" 2>/dev/null
fi
# Also kill any stray onecmd processes (but not this script)
pkill -9 -x onecmd 2>/dev/null
sleep 0.5

if pgrep -x onecmd >/dev/null 2>&1; then
    echo "Error: could not stop onecmd"
    exit 1
fi

echo "Building..."
make || exit 1

echo "Starting onecmd..."
./run.sh "$@" &
echo $! > "$PIDFILE"
echo "onecmd started (pid $!)"
