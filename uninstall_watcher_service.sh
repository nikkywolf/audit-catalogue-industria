#!/bin/zsh
set -e

PLIST="$HOME/Library/LaunchAgents/com.industria.audit-watcher.plist"

launchctl bootout "gui/$(id -u)" "$PLIST" 2>/dev/null || true
rm -f "$PLIST"

echo "Watcher Industria arrêté et retiré du démarrage automatique."
