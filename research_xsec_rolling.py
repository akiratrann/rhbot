"""Rolling walk-forward for cross-sectional momentum.

    python research_xsec_rolling.py

research_xsec.py tested ONE train/test split and found a +101% out-of-sample
edge. One split is one draw. A strategy that works in a single period is
indistinguishable from one that got lucky in that period — momentum in
particular is regime-dependent and crashes hard in reversals.

This re-selects parameters on each training window and scores them on the
following, never-seen window, repeatedly. The question is not "what did the
best window return" but "how often does the edge survive, and how bad is it
when it doesn't".
"""
from __future__ import annotations
import statistics, sys
from research_xsec import (RANGE, aligned_closes, benchmark, load_universe,
                           run_xsec)
from relearn import UNIVERSE

GRID = [(lb, sk, k, rb)
        for lb in (63, 126, 189, 252)
        for sk in (0, 21)
        for k in (3, 5, 8, 12)
        for rb in (21, 63)]

TRAIN_LEN = 252        # 1 year to choose parameters
TEST_LEN = 126         # 6 months to judge them
WARMUP = 273           # longest lookback+skip needs history before ranking


def main() -> int:
    print(f"Loading {len(UNIVERSE)} symbols, {RANGE} daily...")
    dates, closes = aligned_closes(load_universe(UNIVERSE))
    print(f"  {len(closes)} symbols, {len(dates)} sessions "
          f"({dates[0]} -> {dates[-1]})\n")

    folds = []
    start = WARMUP
    while start + TRAIN_LEN + TEST_LEN <= len(dates):
        folds.append((start, start + TRAIN_LEN, start + TRAIN_LEN + TEST_LEN))
        start += TEST_LEN          # non-overlapping test windows
    if not folds:
        print("Not enough history for even one fold.")
        return 1

    print(f"{len(folds)} folds, train {TRAIN_LEN}d / test {TEST_LEN}d, "
          f"test windows do not overlap\n")
    print(f"  {'test window':<26} {'chosen params':<26} {'strat':>9} "
          f"{'hold':>9} {'edge':>9} {'worst wk':>9}")

    edges, strat_rets, hold_rets, worst = [], [], [], []
    for a, b, c in folds:
        best = None
        for lb, sk, k, rb in GRID:
            tot, _, _ = run_xsec(dates, closes, lb, sk, k, rb, a, b)
            if best is None or tot > best[0]:
                best = (tot, lb, sk, k, rb)
        _, lb, sk, k, rb = best
        tot, weekly, _ = run_xsec(dates, closes, lb, sk, k, rb, b, c)
        bh = benchmark(closes, b, c)
        edge = tot - bh
        edges.append(edge); strat_rets.append(tot); hold_rets.append(bh)
        w = min(weekly) if weekly else 0.0
        worst.append(w)
        print(f"  {str(dates[b]) + ' -> ' + str(dates[c-1]):<26} "
              f"{'lb=%d sk=%d k=%d rb=%d' % (lb, sk, k, rb):<26} "
              f"{tot:>+8.2f}% {bh:>+8.2f}% {edge:>+8.2f}% {w:>+8.2f}%")

    wins = sum(1 for e in edges if e > 0)
    print(f"\n  folds beating equal-weight hold : {wins}/{len(edges)} "
          f"({wins/len(edges)*100:.0f}%)")
    print(f"  mean edge                       : {statistics.fmean(edges):+.2f}%")
    print(f"  median edge                     : {statistics.median(edges):+.2f}%")
    print(f"  worst fold edge                 : {min(edges):+.2f}%")
    print(f"  worst week across all folds     : {min(worst):+.2f}%")
    print(f"  mean strategy / mean hold       : "
          f"{statistics.fmean(strat_rets):+.2f}% / "
          f"{statistics.fmean(hold_rets):+.2f}%")
    return 0


if __name__ == "__main__":
    sys.exit(main())
