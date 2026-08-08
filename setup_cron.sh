#!/bin/bash
# Install premarket scanner cron: weekdays 4:00-6:30am PT, every 15 min
# (= 7:00-9:30am ET premarket session)
set -e
DIR="/Users/qfu/Documents/workspace/cameron"
( crontab -l 2>/dev/null | grep -v 'cameron/scanner.py'
  echo "*/15 4-5 * * 1-5 cd $DIR && .venv/bin/python scanner.py >> scan.log 2>&1"
  echo "0,15,30 6 * * 1-5 cd $DIR && .venv/bin/python scanner.py >> scan.log 2>&1"
) | crontab -
echo "installed:"
crontab -l | grep scanner
