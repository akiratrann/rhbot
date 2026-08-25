#!/usr/bin/env bash
# Start LIVE trading on the VPS. This places REAL orders with REAL money.
#
#   ./deploy/go-live.sh root@YOUR_SERVER
#
# Runs pre-flight first and refuses to start if any check fails.
set -euo pipefail
TARGET="${1:?usage: ./deploy/go-live.sh user@host}"
REMOTE=/opt/rhbot

echo "==> pre-flight on the server (places no orders)"
if ! ssh "$TARGET" "cd $REMOTE && sudo -u rhbot .venv/bin/python preflight.py --config config.live.yaml"; then
  echo
  echo "Pre-flight FAILED. Not starting live trading."
  exit 1
fi

echo
read -r -p "Start LIVE trading with real money? type LIVE to confirm: " ans
[ "$ans" = "LIVE" ] || { echo "aborted"; exit 1; }

ssh "$TARGET" "sudo systemctl start rhbot-live.service && sudo systemctl --no-pager --lines=10 status rhbot-live.service"
echo
echo "Live bot running. Stop it with:"
echo "  ssh $TARGET 'sudo systemctl stop rhbot-live.service'"
echo "Emergency halt (keeps the service up but blocks all trading):"
echo "  ssh $TARGET 'sudo touch /opt/rhbot/state/STOP && sudo chown rhbot /opt/rhbot/state/STOP'"
