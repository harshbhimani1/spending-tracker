#!/bin/bash
# Manage the launchd agent that keeps the dashboard (and its sync scheduler)
# running in the background.
#
#   deploy/launchd.sh install     write the agent and start it (also on every login)
#   deploy/launchd.sh uninstall   stop it and remove the agent
#   deploy/launchd.sh status      show whether it is running
#   deploy/launchd.sh plist       print the agent file without installing anything
set -euo pipefail

LABEL="com.spendingtracker.dashboard"
PORT="${SPENDING_PORT:-8765}"
REPO="$(cd "$(dirname "$0")/.." && pwd)"
BACKEND="$REPO/backend"
PYTHON="$BACKEND/.venv/bin/python"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
LOG="$HOME/Library/Logs/spending-tracker.log"
DOMAIN="gui/$(id -u)"

plist_xml() {
  cat <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>$PYTHON</string>
    <string>-m</string>
    <string>uvicorn</string>
    <string>app:app</string>
    <string>--host</string>
    <string>127.0.0.1</string>
    <string>--port</string>
    <string>$PORT</string>
  </array>
  <key>WorkingDirectory</key>
  <string>$BACKEND</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PYTHONUNBUFFERED</key>
    <string>1</string>
  </dict>
  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <true/>
  <key>StandardOutPath</key>
  <string>$LOG</string>
  <key>StandardErrorPath</key>
  <string>$LOG</string>
</dict>
</plist>
EOF
}

case "${1:-}" in
  install)
    if [ ! -x "$PYTHON" ] || ! "$PYTHON" -c "import fastapi, uvicorn" 2>/dev/null; then
      echo "Missing venv or dependencies. From $BACKEND run:" >&2
      echo "  python3 -m venv .venv && .venv/bin/pip install -r requirements.txt" >&2
      exit 1
    fi
    mkdir -p "$(dirname "$PLIST")" "$(dirname "$LOG")"
    plist_xml > "$PLIST"
    plutil -lint "$PLIST" >/dev/null
    # Replace any running copy so re-running install picks up changes.
    launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
    # bootout returns before launchd has finished removing the service, and
    # bootstrapping too early fails with "Input/output error". Wait for it to
    # disappear, then retry the load a few times.
    for _ in $(seq 1 20); do
      launchctl print "$DOMAIN/$LABEL" >/dev/null 2>&1 || break
      sleep 0.5
    done
    for attempt in 1 2 3; do
      launchctl bootstrap "$DOMAIN" "$PLIST" && break
      [ "$attempt" -eq 3 ] && exit 1
      sleep 2
    done
    echo "Installed $LABEL"
    echo "  Dashboard: http://localhost:$PORT"
    echo "  Logs:      $LOG"
    ;;
  uninstall)
    launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
    rm -f "$PLIST"
    echo "Removed $LABEL"
    ;;
  status)
    if launchctl print "$DOMAIN/$LABEL" 2>/dev/null | grep -E "^\s*(state|pid|last exit code|runs) "; then
      echo "Dashboard: http://localhost:$PORT"
    else
      echo "$LABEL is not loaded"
    fi
    ;;
  plist)
    plist_xml
    ;;
  *)
    echo "usage: $0 install|uninstall|status|plist" >&2
    exit 2
    ;;
esac
