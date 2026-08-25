#!/usr/bin/env python3
"""rhbot entry point — start the trading service.

    python run.py                # uses config.yaml
    python run.py --config x.yaml

Ctrl-C stops cleanly and saves state. Panic button while running:
    touch state/STOP
"""

from __future__ import annotations

import argparse
import signal
import sys

from rhbot.config import load_config
from rhbot.dashboard.server import start_dashboard
from rhbot.engine import Engine
from rhbot.factory import (build_feed_and_broker, seed_live_book_from_broker,
                           verify_cash_matches_broker, warn_on_position_drift)
from rhbot.logging_setup import setup_logging
from rhbot.portfolio import Portfolio
from rhbot.risk import RiskManager


def main() -> int:
    ap = argparse.ArgumentParser(description="rhbot trading service")
    ap.add_argument("--config", default="config.yaml")
    args = ap.parse_args()

    cfg = load_config(args.config)
    log = setup_logging(cfg.log_level, cfg.log_file)

    if cfg.is_live:
        log.warning("=" * 60)
        log.warning("LIVE MODE — this will place REAL orders with REAL money.")
        log.warning("=" * 60)

    feed, broker = build_feed_and_broker(cfg)
    # A first deploy has no book. Adopt the broker's real cash and positions
    # rather than inventing paper_starting_cash on a live account.
    seed_live_book_from_broker(cfg, broker, cfg.state_file)
    portfolio = Portfolio.load_or_new(cfg.paper_starting_cash, cfg.state_file)
    # Brokers that know their own positions get checked against the local book
    # before the first tick — exits are sized from the local book.
    warn_on_position_drift(broker, portfolio)
    # Live only: refuse to trade against a book seeded from a default balance.
    verify_cash_matches_broker(broker, portfolio, cfg)
    risk = RiskManager(cfg.risk)
    engine = Engine(cfg, feed, broker, portfolio, risk)

    if cfg.dashboard.enabled:
        start_dashboard(engine, cfg.dashboard.host, cfg.dashboard.port)

    def _handle_sigterm(signum, frame):
        log.info("signal %s received, shutting down", signum)
        engine.stop()

    signal.signal(signal.SIGINT, _handle_sigterm)
    signal.signal(signal.SIGTERM, _handle_sigterm)

    engine.run_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
