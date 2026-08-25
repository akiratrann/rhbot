#!/usr/bin/env bash
# Provision a fresh Ubuntu 22.04/24.04 VPS to run rhbot unattended.
#
#   ssh root@YOUR_SERVER 'bash -s' < deploy/setup.sh
#
# Idempotent — safe to re-run after a code change.
#
# Starts the PAPER service only. The live service is installed but left
# stopped: starting real-money order placement is a deliberate act, not a side
# effect of running a setup script. Enable it with deploy/go-live.sh.
set -euo pipefail

RHBOT_USER=rhbot
RHBOT_HOME=/opt/rhbot

echo "==> packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq python3 python3-venv python3-pip rsync ufw chrony >/dev/null

echo "==> clock"
# Bar timestamps and the session clock are compared against local time. A
# drifting clock makes fresh bars look stale and silently stops all trading.
systemctl enable --now chrony >/dev/null 2>&1 || true

echo "==> user + directories"
id -u "$RHBOT_USER" >/dev/null 2>&1 || useradd --system --home "$RHBOT_HOME" --shell /usr/sbin/nologin "$RHBOT_USER"
mkdir -p "$RHBOT_HOME"/{state,logs}
chown -R "$RHBOT_USER:$RHBOT_USER" "$RHBOT_HOME"

echo "==> python environment"
if [ ! -x "$RHBOT_HOME/.venv/bin/python" ]; then
  python3 -m venv "$RHBOT_HOME/.venv"
fi
"$RHBOT_HOME/.venv/bin/pip" install -q --upgrade pip
"$RHBOT_HOME/.venv/bin/pip" install -q -r "$RHBOT_HOME/requirements.txt"

echo "==> permissions"
# .env holds keys that can trade real money. Owner-read only.
[ -f "$RHBOT_HOME/.env" ] && chmod 600 "$RHBOT_HOME/.env"
chown -R "$RHBOT_USER:$RHBOT_USER" "$RHBOT_HOME"

echo "==> firewall"
# The dashboards bind 127.0.0.1 and must stay unreachable from the internet;
# reach them over an SSH tunnel instead (see deploy/README.md).
ufw allow OpenSSH >/dev/null
ufw --force enable >/dev/null

echo "==> services"
cp "$RHBOT_HOME"/deploy/systemd/*.service "$RHBOT_HOME"/deploy/systemd/*.timer /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now rhbot-paper.service
systemctl enable --now rhbot-relearn.timer
systemctl enable rhbot-live.service        # enabled for boot, NOT started now

echo
echo "==> status"
systemctl --no-pager --lines=0 status rhbot-paper.service || true
echo
echo "Paper bot: running, and restarts on crash or reboot."
echo "Live bot:  installed but STOPPED. Start it with deploy/go-live.sh."
echo
echo "Logs:      tail -f $RHBOT_HOME/logs/paper.log"
echo "Dashboard: ssh -N -L 5001:127.0.0.1:5001 root@YOUR_SERVER  then open"
echo "           http://127.0.0.1:5001"
