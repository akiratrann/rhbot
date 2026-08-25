#!/usr/bin/env bash
# Push the project to the VPS, then restart the paper bot.
#
#   ./deploy/sync.sh root@YOUR_SERVER
#
# Sends .env (it is required and cannot live in git) but never sends local
# state: the server keeps its own book, and overwriting it with a laptop's
# copy is exactly the drift that costs money.
set -euo pipefail
TARGET="${1:?usage: ./deploy/sync.sh user@host}"
REMOTE=/opt/rhbot

cd "$(dirname "$0")/.."

rsync -az --delete \
  --exclude '.venv/' --exclude '__pycache__/' --exclude '.pytest_cache/' \
  --exclude 'state/' --exclude 'logs/' --exclude '.git/' \
  ./ "$TARGET:$REMOTE/"

# .env is excluded from the tree above only by convention; send it explicitly
# with tight permissions so the keys never sit world-readable.
rsync -az --chmod=600 .env "$TARGET:$REMOTE/.env"

ssh "$TARGET" "sudo chown -R rhbot:rhbot $REMOTE && sudo systemctl restart rhbot-paper.service && sudo systemctl --no-pager --lines=5 status rhbot-paper.service"
