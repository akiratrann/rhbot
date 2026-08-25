#!/usr/bin/env python3
"""Intraday parameter sweep for DAY TRADING, using Alpaca bars.

    python sweep_intraday.py --symbols DUOL,TSLA --interval 5m
    python sweep_intraday.py --symbols BTC-USD,ETH-USD --interval 5m --slippage 5

Why this exists separately from `sweep.py`: that one pulls intraday history
from Yahoo, which serves about 5 days of 1-minute bars. Five days is far too
short to tune a day-trading rule on — you end up fitting one week's weather.
Alpaca serves months, and you already have the keys.

Two things this measures that the daily sweep does not have to care about:

  * DAY TRADES PER WEEK. Intraday round trips on stocks are day trades. Under
    $25k equity the cap is 3 per 5 business days, so any stock config above
    ~3/wk is unusable live no matter what it returns. Crypto is exempt.
  * SLIPPAGE SENSITIVITY. A rule doing 200 round trips a month pays the spread
    200 times. Costs that round to nothing on daily bars decide the outcome
    here, which is why --slippage defaults higher than the daily sweep's 2bps.

Historical behaviour, not a prediction, and not a recommendation to trade.
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from typing import List, Optional

from rhbot.alpaca_client import TIMEFRAMES, AlpacaClient
from rhbot.backtest import format_table, run_backtest
from rhbot.config import load_config
from rhbot.models import AssetClass, Bar
from rhbot.strategy.slope_reversal import SlopeReversal

SESSION_MINUTES = 390
MINUTES_PER_WEEK = 60 * 24 * 7
INTERVAL_MINUTES = {"1m": 1, "5m": 5, "15m": 15, "1h": 60}


def fetch(client: AlpacaClient, symbol: str, interval: str,
          is_crypto: bool, limit: int = 10000) -> List[Bar]:
    raw = client.get_bars(symbol, TIMEFRAMES[interval], limit, is_crypto)
    bars: List[Bar] = []
    for b in raw:
        try:
            bars.append(Bar(
                ts=datetime.fromisoformat(
                    b["t"].replace("Z", "+00:00")).astimezone(timezone.utc),
                open=float(b["o"]), high=float(b["h"]), low=float(b["l"]),
                close=float(b["c"]), volume=float(b.get("v", 0) or 0)))
        except (KeyError, TypeError, ValueError):
            continue
    return bars


def weekly_bar_minutes(interval: str, is_crypto: bool) -> float:
    """Minutes to attribute to each bar so 'per week' means a real week.

    Crypto bars cover all 168 hours, so a bar is worth its own duration. Stock
    bars only exist during the 6.5-hour session, so 78 five-minute bars have to
    represent a whole trading day or the per-week rates come out ~4x too low.
    """
    minutes = INTERVAL_MINUTES[interval]
    if is_crypto:
        return float(minutes)
    bars_per_session = SESSION_MINUTES / minutes
    return MINUTES_PER_WEEK / (bars_per_session * 5)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols", required=True,
                    help="comma-separated, e.g. DUOL,TSLA,BTC-USD")
    ap.add_argument("--interval", default="5m", choices=sorted(INTERVAL_MINUTES))
    ap.add_argument("--cash", type=float, default=10_000.0)
    ap.add_argument("--notional", type=float, default=10_000.0)
    # MEASURED from real fills, not assumed. A $20,000 notional BTC order
    # deployed $19,609.95 — 195 bps gone to spread+fee on ONE side. Equity
    # orders lost 0-2 bps on the same day. Defaulting crypto sweeps to 10 bps
    # (the old value) overstated every crypto result by ~20x.
    ap.add_argument("--slippage", type=float, default=None,
                    help="bps per side. Default: 195 for crypto (measured), "
                         "5 for equities. Override to test sensitivity.")
    ap.add_argument("--config", default="config.yaml")
    args = ap.parse_args()

    cfg = load_config(args.config)
    if not cfg.secrets.has_alpaca:
        print("Needs ALPACA_API_KEY / ALPACA_API_SECRET in .env.")
        return 1
    client = AlpacaClient(cfg.secrets.alpaca_api_key,
                          cfg.secrets.alpaca_api_secret, paper=True,
                          data_feed=cfg.alpaca_data_feed)

    for symbol in [s.strip().upper() for s in args.symbols.split(",") if s.strip()]:
        is_crypto = symbol.endswith(("-USD", "-USDT", "-USDC"))
        slippage = args.slippage
        if slippage is None:
            slippage = 195.0 if is_crypto else 5.0
        bars = fetch(client, symbol, args.interval, is_crypto)
        if len(bars) < 200:
            print(f"\n{symbol}: only {len(bars)} bars — skipping (need 200+).")
            continue

        bar_minutes = weekly_bar_minutes(args.interval, is_crypto)
        span_days = (bars[-1].ts - bars[0].ts).days
        bh = (bars[-1].close / bars[0].close - 1.0) * 100.0

        print()
        print("=" * 92)
        print(f"{symbol}  {args.interval} bars  ({len(bars)} bars, "
              f"{bars[0].ts.date()} -> {bars[-1].ts.date()}, ~{span_days}d)  "
              f"slippage {slippage:.0f}bps/side"
              + ("  <- MEASURED from live fills" if args.slippage is None
                 and is_crypto else ""))
        print(f"{'':4}buy & hold over the same window = {bh:+.2f}%"
              + ("" if is_crypto else "   |   PDT cap: DayTr/wk must be < 3"))
        print("=" * 92)

        results = []
        for smooth in (1, 2, 3, 5, 8, 12):
            for dead in (0.0005, 0.001, 0.002, 0.005):
                for cooldown in (0, 3, 12):
                    results.append(run_backtest(
                        f"smooth={smooth} db={dead:.4f} cool={cooldown}bar",
                        SlopeReversal({"smooth": smooth,
                                       "min_slope_pct": dead}),
                        bars, starting_cash=args.cash,
                        order_notional=args.notional,
                        slippage_bps=slippage, cooldown_bars=cooldown,
                        bar_minutes=bar_minutes, symbol=symbol))

        results.sort(key=lambda r: r.return_pct, reverse=True)
        # Live-tradeable means: beats holding, and (for stocks) stays inside the
        # PDT budget. Showing the raw top-10 would mostly list configs that are
        # illegal to run under $25k.
        viable = [r for r in results
                  if r.return_pct > bh
                  and (is_crypto or r.max_day_trades_per_week < 3.0)
                  and r.round_trips >= 5]
        print(format_table(results[:8]))
        print()
        if viable:
            print(f"  VIABLE (beats hold, {'crypto — no PDT cap' if is_crypto else 'under PDT cap'}, 5+ round trips):")
            print(format_table(viable[:5]))
        else:
            gate = ("5+ round trips" if is_crypto
                    else "the PDT cap and 5+ round trips")
            print(f"  NO VIABLE CONFIG: nothing beat buy & hold while meeting "
                  f"{gate}.")
            if is_crypto:
                print("  (crypto is PDT-exempt, so the day-trade column is "
                      "informational only here)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
