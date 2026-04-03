#!/usr/bin/env python3
"""Directly start uvicorn for media manager"""
import subprocess
import sys
import os

os.chdir('/mnt/nas/Secret/media-manager')

# Kill old stuck processes first
subprocess.run(['pkill', '-9', '-f', 'gunicorn.*8765'], capture_output=True)
subprocess.run(['pkill', '-9', '-f', 'uvicorn.*8765'], capture_output=True)

import time
time.sleep(2)

# Start uvicorn directly
proc = subprocess.Popen(
    [sys.executable, '-m', 'uvicorn', 'main:app', '--host', '0.0.0.0', '--port', '8765'],
    stdout=open('server.log', 'a'),
    stderr=subprocess.STDOUT,
    start_new_session=True
)
print(f"Started uvicorn PID: {proc.pid}")

# Wait and test
time.sleep(5)

import socket
s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
result = s.connect_ex(('127.0.0.1', 8765))
s.close()
if result == 0:
    print("✅ Port 8765 is OPEN - server is running!")
else:
    print(f"❌ Port 8765 not responding (error: {result})")
    # Check if process is still alive
    if proc.poll() is None:
        print("  Process is alive but not listening yet, may need more time")
    else:
        print(f"  Process exited with code: {proc.returncode}")
