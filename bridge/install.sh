#!/usr/bin/env bash
# install.sh — Install piread-bridge as a macOS LaunchAgent
set -euo pipefail

PLIST_SRC="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )/com.sam.piread-bridge.plist"
PLIST_DST="$HOME/Library/LaunchAgents/com.sam.piread-bridge.plist"
LABEL="com.sam.piread-bridge"

# Verify boto3 is available
if ! /opt/homebrew/bin/python3 -c "import boto3" 2>/dev/null; then
    echo "ERROR: boto3 not found. Install with: pip3 install boto3"
    exit 1
fi

# Install plist
cp "$PLIST_SRC" "$PLIST_DST"
echo "✓ Installed plist → $PLIST_DST"

# Load (or reload) the agent
if launchctl list | grep -q "$LABEL"; then
    launchctl unload "$PLIST_DST" 2>/dev/null || true
fi
launchctl load "$PLIST_DST"
echo "✓ LaunchAgent loaded"

# Quick health check
sleep 1
if curl -sf http://localhost:7731/ping >/dev/null; then
    echo "✓ Bridge is alive — http://localhost:7731/ping → pong"
else
    echo "⚠ Bridge didn't respond yet — check logs:"
    echo "  tail -f ~/Library/Logs/piread-bridge.log"
fi

echo ""
echo "To manage the bridge:"
echo "  launchctl stop  $LABEL   # stop"
echo "  launchctl start $LABEL   # start"
echo "  launchctl unload $PLIST_DST  # remove from login items"
echo "  tail -f ~/Library/Logs/piread-bridge.log"
