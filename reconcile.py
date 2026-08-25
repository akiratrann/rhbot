#!/usr/bin/env python3
"""Rewrite the local book to match what the broker actually holds.

    python reconcile.py              # show the diff, change nothing
    python reconcile.py --apply      # adopt the broker's positions and cash

The broker is the authority. The local `state/portfolio.json` is a cache, and
it can fall behind — an order that fills while the client has lost the
connection is invisible to it. When that happens the engine sizes exits from a
position it doesn't know about, or blocks entries against cash it thinks it has.

This places NO orders. It only corrects the local accounting.

Cost basis comes from the broker's own average entry price, so realised P&L
stays correct after adoption. Realised P&L to date and the day-trade history
are preserved — they are local history the broker does not track for you.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from typing import Dict

from rhbot.config import load_config
from rhbot.factory import resolve_broker
from rhbot.models import AssetClass
from rhbot.portfolio import Portfolio


def engine_is_running(config_path: str) -> bool:
    """True if an engine using THIS config is alive and would clobber our write.

    Config-specific on purpose. Several engines run side by side (paper, live
    equities, live crypto), each with its own state file. Refusing to reconcile
    the live book because the paper bot happens to be up would block the exact
    repair this tool exists for.
    """
    try:
        out = subprocess.run(["pgrep", "-af", r"python.*run\.py"],
                             capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return False  # can't tell — don't block the user on a failed check

    target = os.path.basename(config_path)
    for line in out.stdout.splitlines():
        if "run.py" not in line:
            continue
        # A bare `run.py` with no --config uses the default config.yaml.
        used = target if f"--config" in line and target in line else None
        if used is None and "--config" not in line and target == "config.yaml":
            used = target
        if used:
            return True
    return False


def broker_snapshot(cfg) -> Dict:
    """Positions and cash straight from the broker, keyed by rhbot symbol."""
    choice = resolve_broker(cfg)
    if choice == "alpaca":
        from rhbot.alpaca_client import AlpacaClient, from_alpaca_symbol
        key, secret = cfg.secrets.alpaca_pair(cfg.is_live)
        client = AlpacaClient(key, secret, paper=not cfg.is_live,
                              data_feed=cfg.alpaca_data_feed)
        acct = client.get_account() or {}
        rows = {}
        for p in client.list_positions():
            raw = str(p.get("symbol", ""))
            if not raw:
                continue
            # Alpaca returns crypto positions concatenated (BTCUSD) and tags
            # them with asset_class; trust the tag, not the symbol's shape.
            is_crypto = str(p.get("asset_class", "")).lower() == "crypto"
            sym = from_alpaca_symbol(raw, is_crypto)
            rows[sym] = {
                "quantity": float(p.get("qty") or 0),
                "avg_price": float(p.get("avg_entry_price") or 0),
                "asset_class": (AssetClass.CRYPTO if is_crypto
                                else AssetClass.STOCK),
            }
        return {"positions": rows, "cash": float(acct.get("cash") or 0)}

    if choice == "schwab":
        from rhbot.brokers.schwab import SchwabBroker
        from rhbot.schwab_client import SchwabClient
        client = SchwabClient(cfg.secrets.schwab_app_key,
                              cfg.secrets.schwab_app_secret)
        if cfg.secrets.schwab_account_number:
            client.select_account(cfg.secrets.schwab_account_number)
        acct = client.get_account(with_positions=True) or {}
        rows = {}
        for p in acct.get("positions") or []:
            sym = ((p.get("instrument") or {}).get("symbol") or "").strip()
            qty = float(p.get("longQuantity") or 0)
            if not sym or qty <= 0:
                continue
            basis = float(p.get("averagePrice") or 0)
            rows[sym] = {"quantity": qty, "avg_price": basis,
                         "asset_class": AssetClass.STOCK}
        cash = float((acct.get("currentBalances") or {})
                     .get("cashBalance") or 0)
        return {"positions": rows, "cash": cash}

    raise SystemExit(f"broker {choice!r} cannot report its own positions — "
                     "nothing to reconcile against.")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--apply", action="store_true",
                    help="write the changes (default is a dry run)")
    ap.add_argument("--state", default=None,
                    help="defaults to the config's state_file")
    args = ap.parse_args()

    cfg = load_config(args.config)
    state_path = args.state or cfg.state_file
    args.state = state_path
    remote = broker_snapshot(cfg)
    local = Portfolio.load_or_new(cfg.paper_starting_cash, state_path)

    local_pos = {s: p.quantity for s, p in local.positions.items()
                 if p.quantity > 0}
    remote_pos = {s: r["quantity"] for s, r in remote["positions"].items()}

    print(f"{'SYMBOL':10} {'BROKER':>16} {'LOCAL':>16}   STATUS")
    drift = False
    for sym in sorted(set(local_pos) | set(remote_pos)):
        r, l = remote_pos.get(sym, 0.0), local_pos.get(sym, 0.0)
        status = "ok" if abs(r - l) < 1e-6 else "DRIFT"
        if status == "DRIFT":
            drift = True
        print(f"{sym:10} {r:>16.6f} {l:>16.6f}   {status}")

    cash_drift = abs(remote["cash"] - local.cash) > 0.01
    print(f"{'cash':10} {remote['cash']:>16,.2f} {local.cash:>16,.2f}   "
          f"{'DRIFT' if cash_drift else 'ok'}")

    if not drift and not cash_drift:
        print("\nBooks agree. Nothing to do.")
        return 0

    if not args.apply:
        print("\nDry run — nothing written. Re-run with --apply to adopt the "
              "broker's numbers.")
        return 1

    # A running engine holds the book in memory and rewrites it on every tick
    # and on shutdown. Reconciling underneath it silently loses this work the
    # moment it next saves.
    if engine_is_running(args.config):
        print("\nREFUSING: run.py is still running. It holds the book in "
              "memory and will overwrite this file on its next save.\n"
              "Stop the engine first, then re-run with --apply, then start it "
              "again.")
        return 2

    raw = {}
    try:
        with open(args.state) as f:
            raw = json.load(f)
    except (OSError, ValueError):
        pass

    raw["cash"] = remote["cash"]
    raw["starting_cash"] = raw.get("starting_cash", cfg.paper_starting_cash)
    raw["realized_pnl"] = raw.get("realized_pnl", 0.0)
    raw["fills"] = raw.get("fills", [])
    raw["day_trade_dates"] = raw.get("day_trade_dates", [])
    raw["positions"] = {
        sym: {
            "symbol": sym,
            "asset_class": r["asset_class"].value,
            "quantity": r["quantity"],
            "avg_price": r["avg_price"],
            # Unknown from the broker. None means "not bought today", which is
            # the safe assumption: it can only ever over-count day trades.
            "last_buy_date": None,
        }
        for sym, r in remote["positions"].items()
    }

    with open(args.state, "w") as f:
        json.dump(raw, f, indent=2)
    print(f"\nAdopted the broker's positions and cash into {args.state}.")
    print("Restart the engine so it reloads the corrected book.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
