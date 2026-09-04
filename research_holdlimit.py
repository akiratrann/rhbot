#!/usr/bin/env python3
"""Does capping the holding period help? Walk-forward, not in-sample.

Entry is the existing slope-reversal signal. Exit is whichever comes first:
the strategy's own exit, or a hard limit of N days.

Why this is worth testing rather than assuming: a 1-2 day hold that crosses
overnight is NOT a day trade, so the PDT cap does not apply. Short holds are
legal at any account size. The only question is whether they pay.
"""
from __future__ import annotations
import statistics, sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from rhbot.strategy.slope_reversal import SlopeReversal
from rhbot.models import Position, AssetClass, SignalType
from sweep import fetch_yahoo

COST = 0.0002          # 2 bps per side, measured from live fills
GRID = [{"smooth": sm, "min_slope_pct": db}
        for sm in (1, 2, 3, 5, 8) for db in (0.0, 0.002, 0.005)]
LIMITS = [1, 2, 3, 5, 10, None]      # None = let the strategy decide


def run(bars, params, max_days):
    st = SlopeReversal(params)
    eq = 1.0; entry = None; held = 0; trips = 0; wins = 0
    c = [b.close for b in bars]
    for i in range(st.warmup_bars, len(bars)):
        pos = (Position("X", AssetClass.STOCK, 1.0, entry) if entry else None)
        sig = st.evaluate("X", bars[:i + 1], pos)
        if entry is not None:
            held += 1
            forced = max_days is not None and held >= max_days
            if sig.type == SignalType.EXIT_LONG or forced:
                r = c[i] / entry * (1 - COST) ** 2
                eq *= r; trips += 1; wins += (r > 1); entry = None; held = 0
        elif sig.type == SignalType.ENTER_LONG:
            entry = c[i]; held = 0
    return (eq - 1) * 100, trips, (wins / trips * 100 if trips else 0)


def one(sym):
    bars = fetch_yahoo(sym, "5y", "1d")
    if not bars or len(bars) < 900:
        return None
    split = int(len(bars) * 0.6)
    train, test = bars[:split], bars[split:]
    out = {"symbol": sym,
           "hold": (test[-1].close / test[0].close - 1) * 100}
    for lim in LIMITS:
        best = None
        for p in GRID:
            r, t, _ = run(train, p, lim)
            if t < 4:
                continue
            if best is None or r > best[0]:
                best = (r, p)
        if best is None:
            out[lim] = None
            continue
        r, t, w = run(test, best[1], lim)
        out[lim] = {"ret": r, "trips": t, "win": w}
    return out


SYMS = ["SMCI","SHOP","META","MSFT","UNH","LMT","DUOL","SNOW","VZ",
        "NVDA","AAPL","TSLA","AMD","PLTR","COIN","NFLX","AVGO","CRM"]
rows = []
with ThreadPoolExecutor(max_workers=8) as pool:
    futs = {pool.submit(one, s): s for s in SYMS}
    for f in as_completed(futs):
        try: r = f.result()
        except Exception: r = None
        if r: rows.append(r)

print(f"\nOUT-OF-SAMPLE across {len(rows)} symbols. Params chosen on train only.")
print("A 1-2 day hold crossing overnight is NOT a day trade — all of these are")
print("legal at $300.\n")
print(f"  {'max hold':>9} {'mean ret':>10} {'median':>9} {'beat hold':>11} "
      f"{'avg trips':>10} {'avg win%':>9}")
for lim in LIMITS:
    vals = [r[lim] for r in rows if r.get(lim)]
    if not vals: continue
    rets = [v["ret"] for v in vals]
    beat = sum(1 for r in rows if r.get(lim) and r[lim]["ret"] > r["hold"])
    label = f"{lim}d" if lim else "no limit"
    print(f"  {label:>9} {statistics.fmean(rets):>+9.2f}% "
          f"{statistics.median(rets):>+8.2f}% {beat:>7}/{len(vals):<3} "
          f"{statistics.fmean(v['trips'] for v in vals):>9.0f} "
          f"{statistics.fmean(v['win'] for v in vals):>8.0f}%")
