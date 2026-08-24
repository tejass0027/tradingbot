#!/bin/sh
set -e

echo "Starting Xvfb on display :99"
Xvfb :99 -screen 0 1280x1024x24 -nolisten tcp &
export DISPLAY=:99

# Give Xvfb a moment to actually come up before Chromium tries to connect,
# instead of relying on xvfb-run's own (possibly hanging) readiness check.
sleep 1

echo "Starting bot"
exec python main.py
