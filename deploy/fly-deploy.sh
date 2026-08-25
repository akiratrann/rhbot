#!/usr/bin/env bash
# Deploy rhbot to Fly.io so it runs with your laptop closed.
#
#   ./deploy/fly-deploy.sh
#
# Reads keys from .env and pushes them straight to Fly as secrets — they are
# piped, never printed, and never written anywhere else.
set -euo pipefail
cd "$(dirname "$0")/.."
APP="${FLY_APP:-rhbot-live}"
# `sea` was retired; `iad` (Ashburn, VA) is the closest live region to the US
# market infrastructure Alpaca sits behind. Override with FLY_REGION.
REGION="${FLY_REGION:-iad}"

command -v flyctl >/dev/null || { echo "flyctl not installed"; exit 1; }
[ -f .env ] || { echo ".env missing"; exit 1; }

echo "==> app"
# Fly app names are globally unique, so "already taken" can mean either "yours,
# from a previous partial run" or "someone else's". Distinguish by asking
# whether WE can see its status.
if flyctl status -a "$APP" >/dev/null 2>&1; then
  echo "    $APP already exists and is ours — reusing"
elif flyctl apps create "$APP" --org personal 2>/dev/null; then
  echo "    created $APP"
else
  echo "ERROR: app name '$APP' is taken by someone else." >&2
  echo "Pick another:  FLY_APP=rhbot-live-akira ./deploy/fly-deploy.sh" >&2
  exit 1
fi

echo "==> volume (holds state/, survives restarts)"
flyctl volumes list -a "$APP" 2>/dev/null | grep -q rhbot_state \
  || flyctl volumes create rhbot_state -a "$APP" --region "$REGION" --size 1 --yes

echo "==> secrets"
# shellcheck disable=SC2046
set -a; . ./.env; set +a
flyctl secrets set -a "$APP" --stage \
  ALPACA_API_KEY="${ALPACA_API_KEY:-}" \
  ALPACA_API_SECRET="${ALPACA_API_SECRET:-}" \
  ALPACA_LIVE_API_KEY="${ALPACA_LIVE_API_KEY:-}" \
  ALPACA_LIVE_API_SECRET="${ALPACA_LIVE_API_SECRET:-}" >/dev/null
echo "    staged (values not echoed)"

echo "==> deploy"
flyctl deploy -a "$APP" --ha=false

echo
echo "Running. Useful commands:"
echo "  fly logs -a $APP                     # live log"
echo "  fly status -a $APP                   # machine state"
echo "  fly proxy 5003:5003 -a $APP          # then open http://127.0.0.1:5003"
echo "  fly ssh console -a $APP -C 'touch /app/state/STOP'   # EMERGENCY HALT"
echo "  fly machine stop -a $APP             # stop trading entirely"
