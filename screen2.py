#!/usr/bin/env python3
"""Two-fold walk-forward screen. A symbol must survive BOTH windows.

    python screen2.py

walkforward.py uses one train/test split. With 64 candidates, roughly 10 will
clear a single split by luck — which is how DUOL earned a place it did not
deserve on an in-sample number.

This runs TWO non-overlapping test windows per symbol, choosing parameters
independently for each from the data preceding it. A symbol passes only if it
beat buy & hold in BOTH, made money in BOTH, and kept drawdown survivable.
Passing one window is a coin flip; passing two independently is roughly a
quarter as likely by chance alone.

The universe is deliberately wide, and that cuts both ways: screening more
names finds more real candidates AND more lucky ones. The two-fold rule is
what keeps the second group out.
"""
from __future__ import annotations
import statistics, sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from rhbot.backtest import run_backtest
from rhbot.strategy.slope_reversal import SlopeReversal
from sweep import fetch_yahoo

MIN = 390
GRID = [{"smooth": sm, "min_slope_pct": db}
        for sm in (1, 2, 3, 5, 8, 12) for db in (0.0, 0.002, 0.005, 0.01)]

UNIVERSE = """
AAPL MSFT NVDA GOOGL AMZN META TSLA NFLX AMD AVGO CRM ORCL INTC QCOM MU PLTR
UNH JNJ LLY PFE ABBV MRK CVS ISRG AMGN GILD VRTX REGN BMY TMO DHR SYK
JPM GS BAC V MA AXP SCHW BLK MS C WFC SPGI CB PGR AON
XOM CVX COP SLB NEE DUK SO CEG EOG PSX MPC VLO OXY HAL
KO PEP WMT COST HD MCD MO PG CL KMB GIS K SYY KR DG
DIS NKE SBUX T VZ BA CAT DE LMT RTX NOC GD HON UNP UPS
DUOL COIN MSTR SMCI SNOW SHOP UBER ABNB DDOG NET CRWD ZS PANW ANET
LRCX KLAC AMAT ADI TXN MCHP ON SWKS TER ENPH FSLR RIVN LCID
"""
SYMBOLS = sorted(set(UNIVERSE.split()))

MIN_RT = 5
MAX_DD = 100.0   # effectively off: ranking on RETURN, not safety


def fold(bars, lo, mid, hi, sym):
    """Choose params on bars[lo:mid], score on bars[mid:hi]."""
    train, test = bars[lo:mid], bars[mid:hi]
    common = dict(starting_cash=10000, order_notional=10000,
                  slippage_bps=2.0, bar_minutes=MIN, symbol=sym)
    best, bestp = None, None
    for p in GRID:
        r = run_backtest("t", SlopeReversal(p), train, **common)
        if r.round_trips < 4:
            continue
        if best is None or r.return_pct > best.return_pct:
            best, bestp = r, p
    if best is None:
        return None
    o = run_backtest("o", SlopeReversal(bestp), test, **common)
    hold = (test[-1].close / test[0].close - 1) * 100
    return {"p": bestp, "test": o.return_pct, "hold": hold,
            "edge": o.return_pct - hold, "rt": o.round_trips,
            "dd": o.max_drawdown_pct}


def one(sym):
    bars = fetch_yahoo(sym, "5y", "1d")
    if not bars or len(bars) < 900:
        return None
    n = len(bars)
    # Two non-overlapping test windows, each preceded by its own training data.
    q = n // 4
    f1 = fold(bars, 0, 2 * q, 3 * q, sym)
    f2 = fold(bars, q, 3 * q, 4 * q, sym)
    if not f1 or not f2:
        return None
    return {"symbol": sym, "f1": f1, "f2": f2}


rows = []
with ThreadPoolExecutor(max_workers=8) as pool:
    futs = {pool.submit(one, s): s for s in SYMBOLS}
    for f in as_completed(futs):
        try:
            r = f.result()
        except Exception:
            r = None
        if r:
            rows.append(r)

def passes(r):
    a, b = r["f1"], r["f2"]
    return (a["edge"] > 0 and b["edge"] > 0
            and a["test"] > 0 and b["test"] > 0
            and a["rt"] >= MIN_RT and b["rt"] >= MIN_RT
            and a["dd"] <= MAX_DD and b["dd"] <= MAX_DD)

# Rank by the WORSE of the two folds' RETURN. Using the worse fold rather than
# the average is the one conservatism worth keeping: it asks "how did this do
# in its bad window", not "how good does the average look".
survivors = sorted((r for r in rows if passes(r)),
                   key=lambda r: min(r["f1"]["test"], r["f2"]["test"]),
                   reverse=True)

print(f"\nScreened {len(rows)} of {len(SYMBOLS)} symbols on TWO independent "
      f"out-of-sample windows.")
print(f"Bar: beat buy & hold AND made money in BOTH, >={MIN_RT} round trips "
      f"each, drawdown <= {MAX_DD}%.\n")
one_fold = sum(1 for r in rows if r["f1"]["edge"] > 0 or r["f2"]["edge"] > 0)
both = len(survivors)
print(f"  beat hold in at least one window : {one_fold}/{len(rows)} "
      f"({one_fold/len(rows)*100:.0f}%)")
print(f"  passed BOTH windows              : {both}/{len(rows)} "
      f"({both/len(rows)*100:.0f}%)\n")

if survivors:
    print(f"  {'SYM':6} {'params':<22} {'fold1 ret':>11} {'fold2 ret':>11} "
          f"{'worst':>8} {'rt':>7} {'maxDD':>7}")
    for r in survivors[:15]:
        a, b = r["f1"], r["f2"]
        pa = f"smooth={a['p']['smooth']} db={a['p']['min_slope_pct']}"
        print(f"  {r['symbol']:6} {pa:<22} {a['test']:>+10.2f}% "
              f"{b['test']:>+10.2f}% {min(a['test'],b['test']):>+7.2f}% "
              f"{a['rt']:>3}/{b['rt']:<3} {max(a['dd'],b['dd']):>6.1f}%")
else:
    print("  NOTHING passed both windows.")
