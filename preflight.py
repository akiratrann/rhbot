#!/usr/bin/env python3
"""Verify a LIVE config end to end without placing a single order.

    python preflight.py --config config.live.yaml

Checks, in order of what actually goes wrong:

  1. Config parses and the live credentials exist (paper keys 401 on the live
     host — a different pair entirely).
  2. The account is reachable, funded, unrestricted, and has enough buying
     power for the configured exposure.
  3. Every watchlist symbol returns a live price AND enough bars for its
     strategy's warmup. A symbol that silently returns no bars never trades.
  4. The local book matches the broker, so exits are sized from reality.
  5. The kill switch is absent (its presence halts everything).

Exit code 0 means every check passed. Non-zero means do not launch.

This places NO orders and moves no money.
"""

from __future__ import annotations

import argparse
import os
import sys

from rhbot.config import load_config
from rhbot.factory import resolve_broker

OK, BAD, WARN = "  ok  ", " FAIL ", " warn "
_failures = 0
_warnings = 0


def check(label: str, passed: bool, detail: str = "", fatal: bool = True):
    global _failures, _warnings
    if passed:
        tag = OK
        detail = ""  # the detail text explains a FAILURE; printing it on a
                     # pass reads as an alarm ("trading is blocked") when the
                     # check actually succeeded.
    elif fatal:
        tag = BAD
        _failures += 1
    else:
        tag = WARN
        _warnings += 1
    print(f"[{tag}] {label}" + (f" — {detail}" if detail else ""))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="config.live.yaml")
    args = ap.parse_args()

    print(f"PRE-FLIGHT: {args.config}\n")

    try:
        cfg = load_config(args.config)
    except Exception as e:  # noqa: BLE001
        print(f"[{BAD}] config failed to load — {e}")
        return 1
    check("config parses", True,
          f"mode={cfg.mode} broker={resolve_broker(cfg)} "
          f"bars={cfg.bar_interval}")

    if not cfg.is_live:
        check("this config is LIVE", False,
              f"mode is {cfg.mode!r} — preflight is for live configs")
        return 1

    if resolve_broker(cfg) != "alpaca":
        check("broker is alpaca", False,
              f"got {resolve_broker(cfg)!r}; preflight only covers Alpaca")
        return 1

    key, secret = cfg.secrets.alpaca_pair(live=True)
    check("live credentials present", bool(key and secret),
          "set ALPACA_LIVE_API_KEY / ALPACA_LIVE_API_SECRET in .env — paper "
          "keys do NOT work on the live host")
    if not (key and secret):
        return 1

    from rhbot.alpaca_client import AlpacaClient
    client = AlpacaClient(key, secret, paper=False,
                          data_feed=cfg.alpaca_data_feed)

    acct = client.get_account()
    check("live account reachable", acct is not None,
          "credentials rejected, or the live host is unreachable")
    if not acct:
        return 1

    equity = float(acct.get("equity") or 0)
    buying_power = float(acct.get("buying_power") or 0)
    status = acct.get("status")

    check("account active", status == "ACTIVE", f"status={status}")
    check("account not restricted",
          not (acct.get("trading_blocked") or acct.get("account_blocked")),
          "trading is blocked on this account")
    check("account funded", equity > 0, f"equity=${equity:,.2f}")
    check("buying power covers exposure",
          buying_power >= cfg.risk.max_total_exposure,
          f"buying power ${buying_power:,.2f} vs max_total_exposure "
          f"${cfg.risk.max_total_exposure:,.2f}",
          fatal=False)

    if equity < 25_000:
        check("PDT guard on (equity under $25k)", cfg.risk.pdt_guard,
              f"equity ${equity:,.2f} — without the guard this account can be "
              f"flagged and restricted")

    total = sum(w.order_notional for w in cfg.watchlist)
    check("configured slots fit the account", total <= equity,
          f"slots total ${total:,.2f} vs equity ${equity:,.2f}", fatal=False)

    # ---- data: a symbol that returns nothing never trades ----
    from rhbot.data.alpaca_feed import AlpacaFeed
    from rhbot.strategy import build_strategy
    symbol_class = {w.symbol: w.asset_class for w in cfg.watchlist}
    feed = AlpacaFeed(client, symbol_class, interval=cfg.bar_interval)

    for w in cfg.watchlist:
        strat = build_strategy(w.strategy, w.params)
        need = strat.warmup_bars + 5
        price = feed.get_price(w.symbol)
        bars = feed.get_bars(w.symbol, need)
        check(f"{w.symbol}: live price", price is not None and price > 0,
              f"got {price!r}")
        check(f"{w.symbol}: {len(bars)}/{need} bars for {w.strategy} warmup",
              len(bars) >= need,
              "not enough history — this symbol will never emit a signal")
        if w.order_notional > equity:
            check(f"{w.symbol}: slot fits equity", False,
                  f"${w.order_notional:,.2f} > ${equity:,.2f}")

    # ---- book vs broker ----
    from rhbot.brokers.alpaca import AlpacaBroker
    from rhbot.portfolio import Portfolio
    broker = AlpacaBroker(client, symbol_class)
    held = broker.held_quantities()
    book = {s: p.quantity for s, p in
            Portfolio.load_or_new(cfg.paper_starting_cash,
                                  cfg.state_file).positions.items()
            if p.quantity > 0}
    matched = all(abs(held.get(s, 0.0) - book.get(s, 0.0)) < 1e-6
                  for s in set(held) | set(book))
    check("local book matches broker", matched,
          f"{cfg.state_file}: local {book or '{}'} vs broker {held or '{}'} — "
          f"stop the engine, then `python reconcile.py --config {args.config} "
          f"--apply`")

    check("kill switch absent", not os.path.exists(cfg.risk.kill_switch_file),
          f"{cfg.risk.kill_switch_file} exists — the engine would halt at once")

    print()
    if _failures:
        print(f"{_failures} FAILURE(S) — do not launch.")
        return 1
    if _warnings:
        print(f"All critical checks passed, {_warnings} warning(s). "
              f"Review them before launching.")
        return 0
    print("All checks passed. Launch with:\n"
          f"    python run.py --config {args.config}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
