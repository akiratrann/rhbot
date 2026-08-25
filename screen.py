#!/usr/bin/env python3
"""Screen many symbols over the past year and report what a WEEK looks like.

    python screen.py --symbols AAPL,MSFT,NVDA
    python screen.py --symbols-file syms.txt --tsv

For each symbol: grid-search slope_reversal on 1 year of daily bars, keep the
best config, then slice its equity curve into calendar weeks.

The weekly distribution is the point. A single "+68% over a year" hides whether
that arrived steadily or in two lucky weeks, and it says nothing about what the
NEXT week is likely to do. Median week, worst week, and share of weeks positive
answer the question a one-week paper run is actually asking.

Historical behaviour, not a prediction.
"""

from __future__ import annotations

import argparse
import statistics
import sys
from collections import OrderedDict
from typing import List, Optional

from rhbot.backtest import run_backtest
from rhbot.strategy.slope_reversal import SlopeReversal
from sweep import fetch_yahoo

MINUTES_PER_TRADING_DAY = 390

SMOOTHS = (1, 2, 3, 5, 8, 12)
DEADBANDS = (0.0, 0.002, 0.005, 0.01)


def weekly_returns(equity_curve) -> List[float]:
    """Percent change of end-of-week equity, week over week."""
    if len(equity_curve) < 2:
        return []
    by_week = OrderedDict()
    for ts, eq in equity_curve:
        y, w, _ = ts.isocalendar()
        by_week[(y, w)] = eq  # last value in each week wins
    closes = list(by_week.values())
    return [(closes[i] / closes[i - 1] - 1.0) * 100.0
            for i in range(1, len(closes)) if closes[i - 1] > 0]


def screen_symbol(symbol: str, cash: float, slippage: float) -> Optional[dict]:
    bars = fetch_yahoo(symbol, range_="1y", interval="1d")
    if not bars or len(bars) < 120:
        return {"symbol": symbol, "error": f"only {len(bars or [])} bars"}

    bh = (bars[-1].close / bars[0].close - 1.0) * 100.0

    best = None
    for smooth in SMOOTHS:
        for dead in DEADBANDS:
            r = run_backtest(
                f"smooth={smooth} db={dead}", SlopeReversal(
                    {"smooth": smooth, "min_slope_pct": dead}),
                bars, starting_cash=cash, order_notional=cash,
                slippage_bps=slippage, bar_minutes=MINUTES_PER_TRADING_DAY,
                symbol=symbol)
            # Require a real sample: a 1-trade config that happens to top the
            # table is buy & hold wearing a costume, not a strategy result.
            if r.round_trips < 4:
                continue
            if best is None or r.return_pct > best.return_pct:
                best, best_smooth, best_dead = r, smooth, dead

    if best is None:
        return {"symbol": symbol, "error": "no config with 4+ round trips"}

    wk = weekly_returns(best.equity_curve)
    return {
        "symbol": symbol,
        "smooth": best_smooth,
        "deadband": best_dead,
        "strat": best.return_pct,
        "hold": bh,
        "edge": best.return_pct - bh,
        "trades": best.trades,
        "round_trips": best.round_trips,
        "win": best.win_rate,
        "maxdd": best.max_drawdown_pct,
        "wk_median": statistics.median(wk) if wk else 0.0,
        "wk_best": max(wk) if wk else 0.0,
        "wk_worst": min(wk) if wk else 0.0,
        "wk_pos": (sum(1 for x in wk if x > 0) / len(wk) * 100.0) if wk else 0.0,
        "wk_n": len(wk),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols", default="")
    ap.add_argument("--cash", type=float, default=10_000.0)
    ap.add_argument("--slippage", type=float, default=2.0)
    ap.add_argument("--tsv", action="store_true",
                    help="machine-readable, for merging parallel runs")
    args = ap.parse_args()

    symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    for sym in symbols:
        try:
            row = screen_symbol(sym, args.cash, args.slippage)
        except Exception as e:  # noqa: BLE001 — one bad symbol must not stop the screen
            row = {"symbol": sym, "error": type(e).__name__}
        if row.get("error"):
            if args.tsv:
                print(f"{sym}\tERROR\t{row['error']}", flush=True)
            else:
                print(f"{sym}: {row['error']}", flush=True)
            continue
        if args.tsv:
            print("\t".join(str(row[k]) for k in
                            ("symbol", "smooth", "deadband", "strat", "hold",
                             "edge", "round_trips", "win", "maxdd",
                             "wk_median", "wk_best", "wk_worst", "wk_pos",
                             "wk_n")), flush=True)
        else:
            print(f"{row['symbol']:6} smooth={row['smooth']:<2} "
                  f"db={row['deadband']:<6} strat={row['strat']:+8.2f}% "
                  f"hold={row['hold']:+8.2f}% edge={row['edge']:+8.2f}% "
                  f"rt={row['round_trips']:<4} win={row['win']:.0f}% "
                  f"dd={row['maxdd']:.1f}% | week med={row['wk_median']:+.2f}% "
                  f"worst={row['wk_worst']:+.2f}% pos={row['wk_pos']:.0f}%",
                  flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
