#!/usr/bin/env python3
"""Cross-sectional momentum: rank the universe, hold the strongest, rebalance.

    python research_xsec.py                      # walk-forward, default universe
    python research_xsec.py --train-frac 0.5

WHY THIS AND NOT ANOTHER PARAMETER GRID
---------------------------------------
Every strategy in this repo so far asks "when should I be in THIS symbol". A
walk-forward over 64 candidates put that family at -9.04% against buy & hold
out of sample: the apparent edge was parameter noise.

This asks a different question — "which of these symbols, right now" — and is
tested against a genuinely hard benchmark: an equal-weight buy-and-hold of the
SAME universe. Beating one stock is easy to do by luck. Beating the average of
all of them, out of sample, is the bar that matters.

Two details that decide whether momentum measurements are real or artefacts:

  * SKIP THE MOST RECENT MONTH when ranking. Short-horizon returns mean-revert,
    so including the last 21 days measures a bounce and rides it the wrong way.
    This is the standard 12-2 formulation, not a tweak.
  * REBALANCE ON A FIXED CALENDAR, never on the signal. Rebalancing when a
    ranking "looks good" is how lookahead sneaks in.

Costs are charged on every position change at the rate MEASURED from real
fills (2 bps equities), not an assumed number.

Historical behaviour, not a prediction.
"""

from __future__ import annotations

import argparse
import statistics
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Dict, List, Optional, Tuple

from relearn import UNIVERSE
from rhbot.models import Bar
from sweep import fetch_yahoo

#: Measured from live fills: equity orders lost 0-2 bps of notional.
EQUITY_COST_BPS = 2.0

#: 5 years, not 2. With a 260-session warmup before the first ranking, a 2-year
#: pull left only ~40 training sessions — parameters chosen on two months of
#: data are not chosen on anything.
RANGE = "5y"


def load_universe(symbols: List[str], workers: int = 8
                  ) -> Dict[str, List[Bar]]:
    out: Dict[str, List[Bar]] = {}
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futs = {pool.submit(fetch_yahoo, s, RANGE, "1d"): s for s in symbols}
        for f in as_completed(futs):
            sym = futs[f]
            try:
                bars = f.result()
            except Exception:  # noqa: BLE001
                bars = None
            if bars and len(bars) > 300:
                out[sym] = bars
    return out


def aligned_closes(data: Dict[str, List[Bar]]) -> Tuple[List, Dict[str, List[float]]]:
    """Common trading dates across all symbols, plus each symbol's closes.

    Momentum compares symbols to each other, so every rank must be computed on
    the same date for all of them. Misaligned series would silently rank a
    symbol's Tuesday against another's Wednesday.
    """
    common = None
    per_sym_by_date = {}
    for sym, bars in data.items():
        by_date = {b.ts.date(): b.close for b in bars}
        per_sym_by_date[sym] = by_date
        dates = set(by_date)
        common = dates if common is None else (common & dates)
    dates = sorted(common or [])
    closes = {s: [per_sym_by_date[s][d] for d in dates] for s in per_sym_by_date}
    return dates, closes


def run_xsec(dates, closes, lookback: int, skip: int, top_k: int,
             rebalance: int, start_idx: int, end_idx: int,
             cost_bps: float = EQUITY_COST_BPS) -> Tuple[float, List[float], int]:
    """Equal-weight top-K by trailing return. Returns (total %, weekly %, turnover)."""
    symbols = sorted(closes)
    equity = 1.0
    held: List[str] = []
    curve: List[Tuple] = []
    turnover = 0
    cost = cost_bps / 10_000.0

    for i in range(start_idx, end_idx):
        if (i - start_idx) % rebalance == 0 and i - lookback - skip >= 0:
            scored = []
            for s in symbols:
                past, recent = closes[s][i - lookback - skip], closes[s][i - skip]
                if past > 0:
                    scored.append((recent / past - 1.0, s))
            scored.sort(reverse=True)
            new = [s for _, s in scored[:top_k]]
            changed = len(set(new) ^ set(held)) / 2 if held else len(new)
            turnover += int(changed)
            equity *= (1 - cost) ** (2 * changed / max(1, top_k))
            held = new

        if held and i + 1 < len(dates):
            day = statistics.fmean(
                closes[s][i + 1] / closes[s][i] - 1.0 for s in held
                if closes[s][i] > 0)
            equity *= (1 + day)
        curve.append((dates[i], equity))

    weekly = []
    for j in range(5, len(curve), 5):
        weekly.append((curve[j][1] / curve[j - 5][1] - 1.0) * 100.0)
    return (equity - 1.0) * 100.0, weekly, turnover


def benchmark(closes, start_idx: int, end_idx: int) -> float:
    """Equal-weight buy & hold of the SAME universe — the honest benchmark."""
    rets = []
    for s in closes:
        a, b = closes[s][start_idx], closes[s][end_idx - 1]
        if a > 0:
            rets.append(b / a - 1.0)
    return statistics.fmean(rets) * 100.0 if rets else 0.0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--train-frac", type=float, default=0.6)
    ap.add_argument("--workers", type=int, default=8)
    args = ap.parse_args()

    print(f"Loading {len(UNIVERSE)} symbols, {RANGE} daily...")
    data = load_universe(UNIVERSE, args.workers)
    dates, closes = aligned_closes(data)
    print(f"  {len(closes)} symbols, {len(dates)} aligned sessions "
          f"({dates[0]} -> {dates[-1]})\n")

    split = int(len(dates) * args.train_frac)
    warmup = 260  # longest lookback + skip needs history before the first rank

    grid = [(lb, sk, k, rb)
            for lb in (63, 126, 189, 252)      # 3, 6, 9, 12 months
            for sk in (0, 21)                  # include vs skip the last month
            for k in (3, 5, 8, 12)
            for rb in (21, 63)]                # monthly vs quarterly

    print(f"TRAIN  {dates[warmup]} -> {dates[split]}   ({len(grid)} configs)")
    trained = []
    for lb, sk, k, rb in grid:
        tot, _, _ = run_xsec(dates, closes, lb, sk, k, rb, warmup, split)
        trained.append((tot, lb, sk, k, rb))
    trained.sort(reverse=True)
    bh_train = benchmark(closes, warmup, split)

    print(f"  equal-weight buy & hold: {bh_train:+.2f}%")
    print(f"  {'lookback':>9} {'skip':>5} {'topK':>5} {'rebal':>6} {'return':>9}")
    for tot, lb, sk, k, rb in trained[:5]:
        print(f"  {lb:>9} {sk:>5} {k:>5} {rb:>6} {tot:>8.2f}%")

    best_tot, lb, sk, k, rb = trained[0]
    print(f"\nTEST   {dates[split]} -> {dates[-1]}   "
          f"(lookback={lb} skip={sk} topK={k} rebal={rb}, chosen on TRAIN only)")
    tot, weekly, turns = run_xsec(dates, closes, lb, sk, k, rb, split, len(dates))
    bh_test = benchmark(closes, split, len(dates))

    print(f"  strategy            {tot:+.2f}%")
    print(f"  equal-weight hold   {bh_test:+.2f}%")
    print(f"  EDGE                {tot - bh_test:+.2f}%")
    if weekly:
        pos = sum(1 for w in weekly if w > 0) / len(weekly) * 100
        print(f"  weekly: median {statistics.median(weekly):+.2f}%  "
              f"worst {min(weekly):+.2f}%  positive {pos:.0f}%  n={len(weekly)}")
    print(f"  rebalances with a change: {turns}")

    print("\n  Sanity check — how the OTHER top-5 train configs did on TEST")
    print("  (if only the winner survives, it was luck, not a signal):")
    for t_tot, t_lb, t_sk, t_k, t_rb in trained[1:5]:
        o, _, _ = run_xsec(dates, closes, t_lb, t_sk, t_k, t_rb, split, len(dates))
        print(f"    lb={t_lb:<4} skip={t_sk:<3} k={t_k:<3} rb={t_rb:<3} "
              f"test {o:+7.2f}%  edge {o - bh_test:+7.2f}%")
    return 0


if __name__ == "__main__":
    sys.exit(main())
