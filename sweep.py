#!/usr/bin/env python3
"""Parameter sweep — compares the three approaches on historical data.

    python sweep.py                  # daily data for the default symbol
    python sweep.py --symbol MSFT
    python sweep.py --csv mydata.csv # your own OHLC csv (ts,open,high,low,close)

Data source: Stooq free daily CSV (no API key). If the network is unavailable it
falls back to SYNTHETIC data and says so loudly — synthetic results are
meaningless for judging a strategy, they only prove the harness runs.

This measures how each rule BEHAVED on past data. It is not a prediction, and
nothing here is a recommendation to trade.
"""

from __future__ import annotations

import argparse
import csv
import io
import sys
from datetime import datetime, timedelta, timezone
from typing import List, Optional

import requests

from rhbot.backtest import format_table, run_backtest
from rhbot.data.feed import SyntheticFeed
from rhbot.models import Bar
from rhbot.strategy.slope_reversal import SlopeReversal
from rhbot.strategy.sma_crossover import SmaCrossover
from rhbot.strategy.swing_trend import SwingTrend
from rhbot.strategy.trend_follow import TrendFollow

MINUTES_PER_TRADING_DAY = 390


def fetch_yahoo(symbol: str, range_: str = "3y",
                interval: str = "1d") -> Optional[List[Bar]]:
    """Free OHLC from Yahoo's public chart endpoint (no API key).

    interval: '1d' for daily, '1m'/'5m' for intraday (intraday history is
    limited to roughly the last 7-60 days depending on interval).
    """
    url = (f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
           f"?range={range_}&interval={interval}")
    try:
        resp = requests.get(url, timeout=25,
                            headers={"User-Agent": "Mozilla/5.0"})
        if resp.status_code != 200:
            return None
        result = resp.json()["chart"]["result"][0]
        stamps = result["timestamp"]
        q = result["indicators"]["quote"][0]
    except (requests.RequestException, KeyError, TypeError, ValueError):
        return None

    bars: List[Bar] = []
    for i, ts in enumerate(stamps):
        c = q["close"][i]
        if c is None:
            continue  # Yahoo emits nulls for halted/missing periods
        bars.append(Bar(
            ts=datetime.fromtimestamp(ts, tz=timezone.utc),
            open=float(q["open"][i] if q["open"][i] is not None else c),
            high=float(q["high"][i] if q["high"][i] is not None else c),
            low=float(q["low"][i] if q["low"][i] is not None else c),
            close=float(c),
            volume=float(q["volume"][i] or 0) if q.get("volume") else 0.0,
        ))
    return bars or None


def load_csv(path: str) -> List[Bar]:
    bars: List[Bar] = []
    with open(path) as f:
        for r in csv.DictReader(f):
            keys = {k.lower(): k for k in r}
            ts_raw = r[keys.get("date") or keys.get("ts")]
            try:
                ts = datetime.fromisoformat(ts_raw.replace("Z", "+00:00"))
            except ValueError:
                ts = datetime.strptime(ts_raw[:10], "%Y-%m-%d")
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            c = float(r[keys["close"]])
            bars.append(Bar(ts=ts, open=float(r.get(keys.get("open", ""), c) or c),
                            high=float(r.get(keys.get("high", ""), c) or c),
                            low=float(r.get(keys.get("low", ""), c) or c),
                            close=c, volume=0.0))
    return bars


def synth_bars(n: int, minutes: int, seed: int = 7) -> List[Bar]:
    feed = SyntheticFeed(seed=seed, base_prices={"TEST": 150.0})
    bars = feed.get_bars("TEST", n)
    # Re-space timestamps to the requested bar interval.
    now = datetime.now(timezone.utc)
    return [Bar(ts=now - timedelta(minutes=minutes * (n - i)), open=b.open,
                high=b.high, low=b.low, close=b.close, volume=b.volume)
            for i, b in enumerate(bars)]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", default="AAPL")
    ap.add_argument("--csv", default=None)
    ap.add_argument("--cash", type=float, default=10_000.0)
    ap.add_argument("--notional", type=float, default=1_000.0)
    args = ap.parse_args()

    synthetic = False
    if args.csv:
        daily = load_csv(args.csv)
        label_src = args.csv
    else:
        daily = fetch_yahoo(args.symbol, range_="3y", interval="1d")
        label_src = f"{args.symbol} daily (yahoo)"
        if not daily:
            synthetic = True
            daily = synth_bars(1200, minutes=MINUTES_PER_TRADING_DAY)
            label_src = "SYNTHETIC daily"

    # Use the last ~3 years of daily bars.
    daily = daily[-750:]

    print()
    print(f"Data: {label_src}  ({len(daily)} bars, "
          f"{daily[0].ts.date()} -> {daily[-1].ts.date()})")
    if synthetic:
        print("!! SYNTHETIC DATA — numbers below are meaningless for judging a")
        print("!! strategy. They only prove the backtest harness works.")
    print()

    common = dict(starting_cash=args.cash, order_notional=args.notional,
                  slippage_bps=2.0, symbol=args.symbol)

    # ---------- A: slope reversal on DAILY bars (PDT-friendly) ----------
    print("=" * 78)
    print("A. SLOPE REVERSAL on DAILY bars — your buy-dip/sell-peak rule, slowed")
    print("   down so overnight holds avoid the day-trade counter.")
    print("=" * 78)
    res_a = []
    for smooth in (1, 2, 3, 5, 8):
        for dead in (0.0, 0.002, 0.005):
            r = run_backtest(
                f"slope smooth={smooth} deadband={dead:.3f}",
                SlopeReversal({"smooth": smooth, "min_slope_pct": dead}),
                daily, bar_minutes=MINUTES_PER_TRADING_DAY, **common)
            res_a.append(r)
    res_a.sort(key=lambda r: r.return_pct, reverse=True)
    print(format_table(res_a))
    print()

    # ---------- B: slope reversal INTRADAY (crypto-style, no PDT) ----------
    print("=" * 78)
    print("B. SLOPE REVERSAL INTRADAY — the high-frequency version. On stocks the")
    print("   DayTr/wk column must stay under 3; crypto is exempt from that cap.")
    print("=" * 78)
    intraday = None if args.csv else fetch_yahoo(
        args.symbol, range_="5d", interval="1m")
    if intraday and len(intraday) > 200:
        # Real bars only cover market hours (390/day, 1950/trading week). Scale
        # the per-bar minute weight so "per week" means a real trading week.
        intraday_minutes = (60 * 24 * 7) / (MINUTES_PER_TRADING_DAY * 5)
        print(f"   (intraday source: {args.symbol} REAL 1-min bars, "
              f"{len(intraday)} bars, last 5 sessions)")
    else:
        intraday = synth_bars(3000, minutes=1, seed=11)
        intraday_minutes = 1.0
        print("   (intraday source: SYNTHETIC 1-min bars — real intraday fetch")
        print("    failed. These B-section numbers are NOT meaningful.)")
    res_b = []
    for smooth in (3, 5, 10):
        for cooldown in (1, 2, 5, 15, 30):
            r = run_backtest(
                f"slope smooth={smooth} cooldown={cooldown}min",
                SlopeReversal({"smooth": smooth, "min_slope_pct": 0.0005}),
                intraday, bar_minutes=intraday_minutes,
                cooldown_bars=cooldown, **common)
            res_b.append(r)
    res_b.sort(key=lambda r: r.return_pct, reverse=True)
    print(format_table(res_b))
    print()

    # ---------- C: swing / multi-day holds ----------
    print("=" * 78)
    print("C. SWING TREND on DAILY bars — designed for multi-day holds, which")
    print("   sidesteps the PDT counter entirely.")
    print("=" * 78)
    res_c = []
    for trend in (20, 50, 100):
        for entry, exit_ in ((30, 70), (35, 65), (40, 60)):
            r = run_backtest(
                f"swing trend={trend} rsi={entry}/{exit_}",
                SwingTrend({"trend": trend, "rsi_entry": entry,
                            "rsi_exit": exit_}),
                daily, bar_minutes=MINUTES_PER_TRADING_DAY, **common)
            res_c.append(r)
    for fast, slow, exit_ma in ((20, 100, 50), (20, 100, 20), (10, 50, 30),
                                (50, 200, 100), (20, 200, 50)):
        for stop in (0.0, 1.0, 2.0):
            r = run_backtest(
                f"trend {fast}/{slow} exit={exit_ma} stop={stop:.0f}R",
                TrendFollow({"fast": fast, "slow": slow, "exit_ma": exit_ma,
                             "stop_atr_mult": stop}),
                daily, bar_minutes=MINUTES_PER_TRADING_DAY, **common)
            res_c.append(r)

    # Baseline for context.
    res_c.append(run_backtest("[baseline] SMA 10/30 crossover",
                              SmaCrossover({"fast": 10, "slow": 30}),
                              daily, bar_minutes=MINUTES_PER_TRADING_DAY, **common))
    res_c.sort(key=lambda r: r.return_pct, reverse=True)
    print(format_table(res_c))
    print()

    # ---------- buy & hold reference ----------
    bh = (daily[-1].close / daily[0].close - 1.0) * 100.0
    print(f"Reference: buy & hold over the same period = {bh:+.2f}%")
    print()
    print("Reminder: these are historical behaviours, not predictions. A config")
    print("that topped this table can still lose money going forward.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
