#!/usr/bin/env python3
"""Walk-forward validation — the only screen result worth believing.

    python walkforward.py --symbols AAPL,MSFT --tsv

`screen.py` picks the best config AFTER seeing the whole year, then scores it on
that same year. That number is always flattering and largely meaningless: with
24 configs per symbol, something will fit the noise.

This splits the year instead. Parameters are chosen using ONLY the training
window, then applied unchanged to a test window the search never saw. If the
edge survives that, it is plausibly real. If it collapses, the screen was
measuring hindsight — which is the usual outcome, and much better to learn here
than with money on it.

Historical behaviour, not a prediction.
"""

from __future__ import annotations

import argparse
import statistics
import sys
from typing import Optional

from rhbot.backtest import run_backtest
from rhbot.strategy.slope_reversal import SlopeReversal
from screen import DEADBANDS, SMOOTHS, weekly_returns
from sweep import fetch_yahoo

MINUTES_PER_TRADING_DAY = 390


def walk_forward(symbol: str, cash: float, slippage: float,
                 train_frac: float) -> dict:
    bars = fetch_yahoo(symbol, range_="2y", interval="1d")
    if not bars or len(bars) < 300:
        return {"symbol": symbol, "error": f"only {len(bars or [])} bars"}

    split = int(len(bars) * train_frac)
    train, test = bars[:split], bars[split:]
    if len(test) < 60:
        return {"symbol": symbol, "error": "test window too short"}

    common = dict(starting_cash=cash, order_notional=cash,
                  slippage_bps=slippage,
                  bar_minutes=MINUTES_PER_TRADING_DAY, symbol=symbol)

    # ---- choose parameters on the TRAINING window only ----
    best = None
    for smooth in SMOOTHS:
        for dead in DEADBANDS:
            r = run_backtest("t", SlopeReversal(
                {"smooth": smooth, "min_slope_pct": dead}), train, **common)
            if r.round_trips < 4:
                continue
            if best is None or r.return_pct > best[0].return_pct:
                best = (r, smooth, dead)
    if best is None:
        return {"symbol": symbol, "error": "no trainable config"}

    train_r, smooth, dead = best

    # ---- apply it, unchanged, to data the search never saw ----
    out = run_backtest("o", SlopeReversal(
        {"smooth": smooth, "min_slope_pct": dead}), test, **common)

    test_hold = (test[-1].close / test[0].close - 1.0) * 100.0
    wk = weekly_returns(out.equity_curve)
    flat = sum(1 for x in wk if abs(x) < 0.01)

    return {
        "symbol": symbol, "smooth": smooth, "deadband": dead,
        "train": train_r.return_pct,
        "test": out.return_pct,
        "test_hold": test_hold,
        "edge": out.return_pct - test_hold,
        "round_trips": out.round_trips,
        "win": out.win_rate,
        "maxdd": out.max_drawdown_pct,
        "wk_mean": statistics.fmean(wk) if wk else 0.0,
        "wk_worst": min(wk) if wk else 0.0,
        "wk_flat_pct": (flat / len(wk) * 100.0) if wk else 0.0,
        "wk_n": len(wk),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols", required=True)
    ap.add_argument("--cash", type=float, default=10_000.0)
    ap.add_argument("--slippage", type=float, default=2.0)
    ap.add_argument("--train-frac", type=float, default=0.6)
    ap.add_argument("--tsv", action="store_true")
    args = ap.parse_args()

    for sym in [s.strip().upper() for s in args.symbols.split(",") if s.strip()]:
        try:
            row = walk_forward(sym, args.cash, args.slippage, args.train_frac)
        except Exception as e:  # noqa: BLE001
            row = {"symbol": sym, "error": type(e).__name__}
        if row.get("error"):
            print(f"{sym}\tERROR\t{row['error']}", flush=True)
            continue
        print("\t".join(str(row[k]) for k in
                        ("symbol", "smooth", "deadband", "train", "test",
                         "test_hold", "edge", "round_trips", "win", "maxdd",
                         "wk_mean", "wk_worst", "wk_flat_pct", "wk_n")),
              flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
