#!/usr/bin/env bash
# One-shot health check of the VPS deployment.
#
#   ./deploy/status.sh root@YOUR_SERVER
set -euo pipefail
TARGET="${1:?usage: ./deploy/status.sh user@host}"
ssh "$TARGET" bash -s <<'REMOTE'
cd /opt/rhbot
for s in rhbot-paper rhbot-live; do
  printf '%-14s %s\n' "$s" "$(systemctl is-active $s 2>/dev/null) (enabled: $(systemctl is-enabled $s 2>/dev/null))"
done
printf '%-14s %s\n' "relearn timer" "$(systemctl is-active rhbot-relearn.timer 2>/dev/null)"
echo "--- next relearn ---"; systemctl list-timers rhbot-relearn.timer --no-pager 2>/dev/null | sed -n 2p
echo "--- kill switch ---"; [ -f state/STOP ] && echo "PRESENT — trading halted" || echo "absent"
for port in 5001 5003; do
  echo "--- dashboard :$port ---"
  curl -s --max-time 5 "http://127.0.0.1:$port/api/state" \
    | .venv/bin/python -c "import json,sys;d=json.load(sys.stdin);print(f\"  equity \${d['equity']:,.2f} realized \${d['realized_pnl']:,.2f} unrealized \${d['unrealized_pnl']:,.2f} ticks {d['tick_count']} halted={d['halted']}\")" 2>/dev/null \
    || echo "  not running"
done
echo "--- recent errors (24h) ---"
grep -hc ERROR logs/*.log 2>/dev/null | paste -sd+ | bc 2>/dev/null || echo 0
REMOTE
