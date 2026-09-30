#!/bin/bash
# Registers a launchd job that runs the robot at 8:00 and 18:00 every day.
# If the Mac was asleep at that time, it runs when it wakes.   Run once:  bash schedule-mac.sh
DIR="$(cd "$(dirname "$0")" && pwd)"
PY="$(command -v python3)"
PLIST="$HOME/Library/LaunchAgents/com.guc-deadlines.plist"
mkdir -p "$HOME/Library/LaunchAgents" "$DIR/data"
cat > "$PLIST" <<PL
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>com.guc-deadlines</string>
  <key>ProgramArguments</key><array><string>$PY</string><string>$DIR/run.py</string></array>
  <key>WorkingDirectory</key><string>$DIR</string>
  <key>StartCalendarInterval</key><array>
    <dict><key>Hour</key><integer>8</integer><key>Minute</key><integer>0</integer></dict>
    <dict><key>Hour</key><integer>18</integer><key>Minute</key><integer>0</integer></dict>
  </array>
  <key>StandardOutPath</key><string>$DIR/data/launchd.log</string>
  <key>StandardErrorPath</key><string>$DIR/data/launchd.log</string>
</dict></plist>
PL
launchctl unload "$PLIST" 2>/dev/null
launchctl load "$PLIST"
echo "Done. The robot will run at 8:00 and 18:00 (or when your Mac wakes up after those times)."
echo "To run it right now: launchctl start com.guc-deadlines"
