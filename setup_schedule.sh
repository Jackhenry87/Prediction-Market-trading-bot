#!/bin/bash
# Installs a macOS launchd job that runs auto_trade.py automatically
# every day at 7:00, 9:00, 12:00, and 15:00 (local time), when the Mac
# is awake.
#
#   bash setup_schedule.sh            install / update the schedule
#   bash setup_schedule.sh remove     stop and remove the schedule
#
# To pause trading WITHOUT touching the schedule: set KILL_SWITCH=true
# in .env — scheduled runs keep happening but refuse to place orders.

set -e
PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
PLIST="$HOME/Library/LaunchAgents/com.prediction-bot.autotrade.plist"

if [ "$1" = "remove" ]; then
    launchctl unload "$PLIST" 2>/dev/null || true
    rm -f "$PLIST"
    echo "Schedule removed. The bot will only run when you run it manually."
    exit 0
fi

mkdir -p "$PROJECT_DIR/logs" "$HOME/Library/LaunchAgents"

cat > "$PLIST" << EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>com.prediction-bot.autotrade</string>
  <key>ProgramArguments</key>
  <array>
    <string>$PROJECT_DIR/venv/bin/python</string>
    <string>$PROJECT_DIR/auto_trade.py</string>
  </array>
  <key>WorkingDirectory</key><string>$PROJECT_DIR</string>
  <key>StartCalendarInterval</key>
  <array>
    <dict><key>Hour</key><integer>7</integer><key>Minute</key><integer>0</integer></dict>
    <dict><key>Hour</key><integer>9</integer><key>Minute</key><integer>0</integer></dict>
    <dict><key>Hour</key><integer>12</integer><key>Minute</key><integer>0</integer></dict>
    <dict><key>Hour</key><integer>15</integer><key>Minute</key><integer>0</integer></dict>
  </array>
  <key>StandardOutPath</key><string>$PROJECT_DIR/logs/schedule_stdout.log</string>
  <key>StandardErrorPath</key><string>$PROJECT_DIR/logs/schedule_stderr.log</string>
</dict>
</plist>
EOF

launchctl unload "$PLIST" 2>/dev/null || true
launchctl load "$PLIST"
echo "Installed: auto_trade.py runs daily at 7:00, 9:00, 12:00 and 15:00 while the Mac is awake."
echo "Each run also writes its own timestamped log in logs/."
echo "Pause trading anytime: set KILL_SWITCH=true in .env"
echo "Remove the schedule:   bash setup_schedule.sh remove"
