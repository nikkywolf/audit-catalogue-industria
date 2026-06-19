#!/bin/zsh
set -e

PROJECT_DIR="/Users/industriacoiffure/industria-apps/audit-catalogue-industria"
PLIST_NAME="com.industria.audit-watcher.plist"
SOURCE_PLIST="$PROJECT_DIR/launchd/$PLIST_NAME"
TARGET_DIR="$HOME/Library/LaunchAgents"
TARGET_PLIST="$TARGET_DIR/$PLIST_NAME"

mkdir -p "$TARGET_DIR"
mkdir -p "$PROJECT_DIR/logs"

launchctl bootout "gui/$(id -u)" "$TARGET_PLIST" 2>/dev/null || true
cp "$SOURCE_PLIST" "$TARGET_PLIST"
launchctl bootstrap "gui/$(id -u)" "$TARGET_PLIST"
launchctl enable "gui/$(id -u)/com.industria.audit-watcher"
launchctl kickstart -k "gui/$(id -u)/com.industria.audit-watcher"

echo "Watcher Industria installé et démarré."
echo "Logs: $PROJECT_DIR/logs/audit-watcher.log"
