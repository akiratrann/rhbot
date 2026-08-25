#!/usr/bin/env python3
"""Weekly re-selection: walk-forward a universe, keep what survives, log it.

    python relearn.py                       # study, print, write nothing
    python relearn.py --apply config.yaml   # rewrite that config's watchlist

Run it weekly (cron / launchd). What it does:

  1. Walk-forward every candidate: choose params on a TRAINING window, then
     score them on data the search never saw.
  2. Keep only symbols that, out of sample, made money AND beat buy & hold AND
     traded enough times to be a sample rather than an accident.
  3. Append the decision to state/relearn_history.jsonl.

WHAT THIS FIXES AND WHAT IT DOES NOT
------------------------------------
Fixes: parameters going stale as a symbol's character changes, and the watchlist
silently becoming a museum of whatever worked once.

Does NOT fix: the underlying strategy having no edge. Re-selecting weekly from a
large universe is itself a multiple-comparisons machine — screen 80 symbols and
a few will look good by luck alone. The history log exists precisely so you can
check whether last week's picks actually delivered. If chosen symbols routinely
underperform their backtest, the honest conclusion is that the selection is
noise, and no amount of re-running will change that.

Historical behaviour, not a prediction.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from typing import Dict, List, Optional

from sweep import fetch_yahoo
from walkforward import walk_forward

HISTORY = "state/relearn_history.jsonl"

#: Liquid, diverse candidates. Not a recommendation list — it is a search space
#: wide enough that the filter has something to reject.
UNIVERSE = [
    "AAPL", "MSFT", "NVDA", "GOOGL", "AMZN", "META", "TSLA", "NFLX",
    "AMD", "AVGO", "CRM", "ORCL", "INTC", "QCOM", "MU", "PLTR",
    "UNH", "JNJ", "LLY", "PFE", "ABBV", "MRK", "CVS", "ISRG",
    "JPM", "GS", "BAC", "V", "MA", "AXP", "SCHW", "BLK",
    "XOM", "CVX", "COP", "SLB", "NEE", "DUK", "SO", "CEG",
    "KO", "PEP", "WMT", "COST", "HD", "MCD", "MO", "PG",
    "DIS", "NKE", "SBUX", "T", "VZ", "BA", "CAT", "DE",
    "DUOL", "COIN", "MSTR", "SMCI", "SNOW", "SHOP", "UBER", "ABNB",
]

#: A config is only kept if it clears ALL of these out of sample.
MIN_ROUND_TRIPS = 6      # fewer than this is an anecdote, not a sample
MIN_TEST_RETURN = 0.0    # beating a -40% hold with -27% is still losing money
MIN_EDGE = 0.0           # must beat simply holding the thing
MAX_DRAWDOWN = 35.0      # no matter the return, this has to be survivable


def wait_for_data(attempts: int = 6, delay: int = 20,
                  sleep=None) -> bool:
    """Confirm the data source answers before studying anything.

    launchd/systemd fire this job the moment the machine wakes, often before
    the network is up. Every fetch then fails, and a study of zero symbols
    would otherwise be reported as "no edge" — a conclusion about the market
    drawn from a conclusion about the wifi.
    """
    import time as _t
    sleep = sleep or _t.sleep
    for i in range(attempts):
        if fetch_yahoo("AAPL", range_="1mo", interval="1d"):
            return True
        if i < attempts - 1:
            print(f"  data source unreachable, retry {i + 1}/{attempts - 1}...")
            sleep(delay)
    return False


def study(universe: List[str], workers: int, train_frac: float,
          slippage: float) -> List[dict]:
    rows: List[dict] = []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(walk_forward, s, 10_000.0, slippage,
                               train_frac): s for s in universe}
        for fut in as_completed(futures):
            sym = futures[fut]
            try:
                row = fut.result()
            except Exception as e:  # noqa: BLE001 — one bad symbol can't stop the study
                row = {"symbol": sym, "error": type(e).__name__}
            rows.append(row)
    return rows


def survivors(rows: List[dict]) -> List[dict]:
    out = [r for r in rows
           if not r.get("error")
           and r["round_trips"] >= MIN_ROUND_TRIPS
           and r["test"] > MIN_TEST_RETURN
           and r["edge"] > MIN_EDGE
           and r["maxdd"] <= MAX_DRAWDOWN]
    return sorted(out, key=lambda r: r["edge"], reverse=True)


def render_watchlist(picks: List[dict], notional: float,
                     include_crypto: bool) -> str:
    lines = ["watchlist:"]
    for p in picks:
        lines += [
            f"  # walk-forward: test {p['test']:+.2f}% vs hold "
            f"{p['test_hold']:+.2f}% (edge {p['edge']:+.2f}%), "
            f"{p['round_trips']} round trips, {p['maxdd']:.1f}% max DD",
            f"  - symbol: {p['symbol']}",
            f"    asset_class: stock",
            f"    strategy: slope_reversal",
            f"    params:",
            f"      smooth: {p['smooth']}",
            f"      min_slope_pct: {p['deadband']}",
            f"    order_notional: {notional}",
            "",
        ]
    if include_crypto:
        lines += [
            "  # Crypto is not walk-forward screened here (the equity universe",
            "  # above is). It is present for PDT exemption and 24/7 coverage.",
            "  - symbol: BTC-USD",
            "    asset_class: crypto",
            "    strategy: slope_reversal",
            "    params:",
            "      smooth: 5",
            "      min_slope_pct: 0.002",
            f"    order_notional: {notional}",
            "",
        ]
    return "\n".join(lines)


def apply_to_config(path: str, watchlist_yaml: str) -> None:
    with open(path) as f:
        text = f.read()
    start = text.index("watchlist:")
    end = text.index("# ------------------------------------------------------------\n#  Risk limits")
    with open(path, "w") as f:
        f.write(text[:start] + watchlist_yaml + text[end:])


def log_history(rows: List[dict], picks: List[dict], stamp: str) -> None:
    os.makedirs(os.path.dirname(HISTORY), exist_ok=True)
    entry = {
        "ts": stamp,
        "studied": len([r for r in rows if not r.get("error")]),
        "survived": len(picks),
        "picks": [{k: p[k] for k in
                   ("symbol", "smooth", "deadband", "test", "test_hold",
                    "edge", "round_trips", "maxdd")} for p in picks],
    }
    with open(HISTORY, "a") as f:
        f.write(json.dumps(entry) + "\n")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", metavar="CONFIG", default=None,
                    help="rewrite this config's watchlist with the survivors")
    ap.add_argument("--top", type=int, default=4,
                    help="how many equity slots to keep")
    ap.add_argument("--notional", type=float, default=500.0)
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--train-frac", type=float, default=0.6)
    ap.add_argument("--slippage", type=float, default=2.0)
    ap.add_argument("--no-crypto", action="store_true")
    ap.add_argument("--stamp", default=None,
                    help="ISO timestamp for the history log (defaults to now)")
    args = ap.parse_args()

    stamp = args.stamp or datetime.now(timezone.utc).isoformat(timespec="seconds")

    if not wait_for_data():
        print("ABORTING: the market-data source is unreachable, so nothing "
              "could be studied.\nThis says nothing about the strategy — it is "
              "a network failure. Config left untouched.")
        return 3

    print(f"Studying {len(UNIVERSE)} candidates "
          f"(train {args.train_frac:.0%} / test {1 - args.train_frac:.0%})...")

    rows = study(UNIVERSE, args.workers, args.train_frac, args.slippage)
    ok = [r for r in rows if not r.get("error")]
    kept = survivors(rows)
    picks = kept[:args.top]

    print(f"\nstudied {len(ok)}, survived all filters: {len(kept)} "
          f"({len(kept) / max(1, len(ok)) * 100:.0f}%)")
    print(f"filters: >={MIN_ROUND_TRIPS} round trips, test return > "
          f"{MIN_TEST_RETURN}%, edge > {MIN_EDGE}%, max DD <= {MAX_DRAWDOWN}%\n")

    if not ok:
        # Zero symbols STUDIED is a data failure, not a market finding. Saying
        # "no edge" here would be drawing a conclusion about the strategy from
        # a broken fetch — the exact mistake this whole project keeps guarding
        # against.
        errs = {}
        for r in rows:
            errs[r.get("error", "?")] = errs.get(r.get("error", "?"), 0) + 1
        print("ABORTING: 0 of %d candidates could be studied — every fetch "
              "failed.\nThis is a DATA problem, not a strategy result. "
              "Errors: %s\nConfig left untouched."
              % (len(rows), dict(sorted(errs.items(), key=lambda x: -x[1]))))
        return 3

    if not kept:
        print("Nothing cleared the filters this week. That IS a result: on "
              "this universe,\nno config beat buy & hold out of sample with a "
              "real trade sample.\nLeaving the config untouched.")
        log_history(rows, [], stamp)
        return 2

    print(f"{'SYM':6} {'CONFIG':22} {'TEST%':>8} {'HOLD%':>8} {'EDGE%':>8} "
          f"{'RT':>4} {'DD%':>6}")
    for r in kept[:12]:
        marker = " <-" if r in picks else ""
        print(f"{r['symbol']:6} smooth={r['smooth']:<2} db={r['deadband']:<7} "
              f"{r['test']:+8.2f} {r['test_hold']:+8.2f} {r['edge']:+8.2f} "
              f"{r['round_trips']:>4} {r['maxdd']:>6.1f}{marker}")

    log_history(rows, picks, stamp)
    print(f"\nlogged to {HISTORY}")

    yaml_text = render_watchlist(picks, args.notional, not args.no_crypto)
    if args.apply:
        apply_to_config(args.apply, yaml_text)
        print(f"applied {len(picks)} equity slots to {args.apply}")
        print("Stop the engine, run `python reconcile.py --apply`, then restart.")
    else:
        print("\n--- watchlist (not applied; pass --apply CONFIG to write) ---")
        print(yaml_text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
