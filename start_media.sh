#!/bin/bash
# Kill old processes
pkill -9 -f "gunicorn.*8765" 2>/dev/null
sleep 1

# Start media manager
cd /mnt/nas/Secret/media-manager
gunicorn main:app -w 1 -k uvicorn.workers.UvicornWorker -b 0.0.0.0:8765 --timeout 120 >> server.log 2>&1 &
PID=$!
echo "Started PID: $PID"
echo $PID > /tmp/media_manager.pid
