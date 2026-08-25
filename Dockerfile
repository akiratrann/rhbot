# Runs rhbot 24/7 on Fly.io. No dashboard port is published: the dashboard has
# no authentication and shows your positions, so it stays reachable only over
# `fly ssh console` / `fly proxy`.
FROM python:3.12-slim

# Bar freshness is compared against the clock. A drifting clock makes live bars
# look stale and silently stops all trading, so keep tzdata current.
RUN apt-get update -qq && apt-get install -y -qq --no-install-recommends \
      tzdata ca-certificates && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -q -r requirements.txt

COPY rhbot/ ./rhbot/
COPY run.py preflight.py reconcile.py relearn.py walkforward.py screen.py \
     sweep.py sweep_intraday.py ./
COPY config.live.yaml config.live.crypto.yaml config.yaml ./

# The book lives on a Fly volume mounted here, so a machine restart or a
# redeploy does not resurrect a stale portfolio from the image.
RUN mkdir -p /app/state /app/logs
ENV PYTHONUNBUFFERED=1

CMD ["python", "run.py", "--config", "config.live.yaml"]
