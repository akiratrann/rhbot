"""Walk-forward: two-point difference vs least-squares derivative.

Same protocol for both — params chosen on the training window, scored on data
the search never saw. In-sample comparisons of two strategies are meaningless:
whichever has more parameters wins by fitting more noise.
"""
from __future__ import annotations
import statistics, sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from rhbot.backtest import run_backtest
from rhbot.strategy.slope_reversal import SlopeReversal
from rhbot.strategy.slope_regression import SlopeRegression
from relearn import UNIVERSE
UNIVERSE = UNIVERSE[:28]  # trimmed: the full grid over 64 symbols exceeds the run budget
from sweep import fetch_yahoo

MIN = 390
DIFF_GRID = [{"smooth": sm, "min_slope_pct": db}
             for sm in (1, 2, 3, 5, 8, 12) for db in (0.0, 0.002, 0.005, 0.01)]
REG_GRID = [{"window": w, "min_slope_pct": db, "require_curvature": rc}
            for w in (5, 10, 15, 20) for db in (0.0, 0.001)
            for rc in (False, True)]

def one(sym):
    bars = fetch_yahoo(sym, "5y", "1d")
    if not bars or len(bars) < 500:
        return None
    split = int(len(bars) * 0.6)
    train, test = bars[:split], bars[split:]
    common = dict(starting_cash=10000, order_notional=10000,
                  slippage_bps=2.0, bar_minutes=MIN, symbol=sym)
    hold = (test[-1].close / test[0].close - 1) * 100
    out = {"symbol": sym, "hold": hold}
    for tag, cls, grid in (("diff", SlopeReversal, DIFF_GRID),
                           ("reg", SlopeRegression, REG_GRID)):
        best, bestp = None, None
        for p in grid:
            try:
                r = run_backtest("t", cls(p), train, **common)
            except ValueError:
                continue
            if r.round_trips < 4:
                continue
            if best is None or r.return_pct > best.return_pct:
                best, bestp = r, p
        if best is None:
            out[tag] = None
            continue
        o = run_backtest("o", cls(bestp), test, **common)
        out[tag] = {"test": o.return_pct, "edge": o.return_pct - hold,
                    "trades": o.trades, "dd": o.max_drawdown_pct}
    return out

rows = []
with ThreadPoolExecutor(max_workers=8) as pool:
    futs = {pool.submit(one, s): s for s in UNIVERSE}
    for f in as_completed(futs):
        try:
            r = f.result()
        except Exception:
            r = None
        if r: rows.append(r)

def summarise(tag):
    vals = [r[tag] for r in rows if r.get(tag)]
    if not vals: return
    edges = [v["edge"] for v in vals]
    beat = sum(1 for e in edges if e > 0)
    made = sum(1 for v in vals if v["test"] > 0)
    both = sum(1 for v in vals if v["test"] > 0 and v["edge"] > 0)
    print(f"  {tag:5} n={len(vals):3}  mean edge {statistics.fmean(edges):+7.2f}%  "
          f"median {statistics.median(edges):+7.2f}%  beat hold {beat:>2}/{len(vals)} "
          f"({beat/len(vals)*100:.0f}%)  made money {made:>2}  BOTH {both:>2} "
          f"({both/len(vals)*100:.0f}%)  avg trades {statistics.fmean(v['trades'] for v in vals):.0f}")

print(f"OUT-OF-SAMPLE across {len(rows)} symbols (params chosen on train only)\n")
summarise("diff"); summarise("reg")
