#!/bin/bash
# Kill existing media manager processes
pkill -9 -f "gunicorn.*8765" 2>/dev/null
pkill -9 -f "uvicorn.*8765" 2>/dev/null
sleep 1

# Start fresh
cd /mnt/nas/Secret/media-manager
nohup gunicorn main:app -w 1 -k uvicorn.workers.UvicornWorker -b 0.0.0.0:8765 --timeout 120 >> server.log 2>&1 &
echo "Started PID: $!"

# Wait and test
sleep 3
curl -s -m 5 -o /dev/null -w "HTTP %{http_code}" http://127.0.0.1:8765/ || echo "Still not responding"
