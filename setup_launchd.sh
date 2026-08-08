#!/bin/bash
# Install launchd agents: auto-start on boot/login + auto-restart on crash.
# The running nohup instances are killed first; launchd takes over.
set -e
DIR="/Users/qfu/cameron"
mkdir -p ~/Library/LaunchAgents

# stop current nohup instances so launchd can grab the lock cleanly
lsof -t "$DIR/.autotrader.lock" 2>/dev/null | xargs kill 2>/dev/null || true
pgrep -f 'dashboard.py' | xargs kill 2>/dev/null || true
sleep 2

for name in autotrader dashboard; do
  cp "$DIR/com.cameron.$name.plist" ~/Library/LaunchAgents/
  launchctl unload ~/Library/LaunchAgents/com.cameron.$name.plist 2>/dev/null || true
  launchctl load ~/Library/LaunchAgents/com.cameron.$name.plist
done
sleep 3
echo "--- status ---"
launchctl list | grep com.cameron
echo "--- verify ---"
tail -1 "$DIR/autotrader.log"
curl -s -o /dev/null -w "dashboard HTTP %{http_code}\n" http://localhost:8787/
