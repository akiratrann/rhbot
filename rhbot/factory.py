"""Wires feeds and brokers from config + available credentials.

Rules:
  - `broker` picks the venue (see config.BROKERS); `auto` prefers Alpaca when
    its keys are present because it is the only officially supported API here.
  - paper mode: use real feeds if creds are present, else fall back to free
    Yahoo data, else the credential-free SyntheticFeed (with a loud warning).
  - live mode: require the matching credentials, or refuse to start.

Note the asymmetry between the two paper paths. `broker: paper` simulates fills
locally. `broker: alpaca` in paper mode sends orders to Alpaca's paper host — a
real matching engine holding fake money, with no PDT cap and no settlement. The
second is a much better rehearsal, which is why `auto` prefers it.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Dict, Optional, Tuple

from .brokers.base import Broker
from .brokers.paper import PaperBroker
from .config import Config
from .data.feed import CompositeFeed, DataFeed, SyntheticFeed
from .models import AssetClass
from .portfolio import market_date

log = logging.getLogger("rhbot.factory")

#: Below this equity a US margin account is capped at 3 day trades / 5 days.
PDT_EQUITY_THRESHOLD = 25_000.0


def resolve_broker(cfg: Config) -> str:
    """Turn `broker: auto` into a concrete choice. Never returns 'auto'.

    `auto` deliberately never selects Schwab: that API is live-money-only, so
    routing real orders there has to be an explicit `broker: schwab`.
    """
    if cfg.broker != "auto":
        return cfg.broker
    if cfg.secrets.has_alpaca:
        return "alpaca"
    if cfg.is_live:
        return "robinhood"
    return "paper"


def build_feed_and_broker(cfg: Config) -> Tuple[DataFeed, Broker]:
    symbol_class = {w.symbol: w.asset_class for w in cfg.watchlist}
    classes = set(symbol_class.values())

    choice = resolve_broker(cfg)
    if choice == "alpaca":
        return _build_alpaca(cfg, symbol_class)
    if choice == "schwab":
        return _build_schwab(cfg)

    feeds: Dict[AssetClass, DataFeed] = {}
    have_stock_creds = bool(cfg.secrets.rh_username and cfg.secrets.rh_password)
    have_crypto_creds = bool(
        cfg.secrets.rh_crypto_api_key and cfg.secrets.rh_crypto_private_key
    )

    crypto_client = None

    # ---- real feeds where we can ----
    if AssetClass.STOCK in classes and have_stock_creds:
        _login_stock(cfg)
        from .data.rh_stock_feed import RobinhoodStockFeed
        feeds[AssetClass.STOCK] = RobinhoodStockFeed()

    if AssetClass.CRYPTO in classes and have_crypto_creds:
        from .rh_crypto_client import RobinhoodCryptoClient
        from .data.rh_crypto_feed import RobinhoodCryptoFeed
        crypto_client = RobinhoodCryptoClient(
            cfg.secrets.rh_crypto_api_key, cfg.secrets.rh_crypto_private_key
        )
        feeds[AssetClass.CRYPTO] = RobinhoodCryptoFeed(crypto_client)

    # ---- fallback for classes without credentials (paper only) ----
    missing = classes - set(feeds.keys())
    if missing:
        if cfg.is_live:
            raise SystemExit(
                f"LIVE mode needs real data for {sorted(a.value for a in missing)} "
                f"but credentials are missing. Add them to .env or switch to paper."
            )
        # Prefer REAL free data (Yahoo) over synthetic so paper mode is
        # meaningful with zero setup. Synthetic is the last resort.
        from .data.yahoo_feed import YahooFeed
        fallback: DataFeed = YahooFeed(interval=cfg.bar_interval,
                                       range_=cfg.yahoo_range)
        probe_symbol = next(w.symbol for w in cfg.watchlist
                            if w.asset_class in missing)
        if fallback.get_price(probe_symbol) is not None:
            log.info("No credentials for %s — using free Yahoo market data "
                     "(real prices, paper execution), %s bars.",
                     sorted(a.value for a in missing), cfg.bar_interval)
        else:
            log.warning("No credentials and Yahoo unreachable — falling back to "
                        "SYNTHETIC (fake) data. Results are meaningless.")
            fallback = SyntheticFeed(
                base_prices={w.symbol: 100.0 for w in cfg.watchlist}
            )
        for ac in missing:
            feeds[ac] = fallback

    feed = CompositeFeed(feeds, symbol_class)

    # ---- broker ----
    if not cfg.is_live or choice == "paper":
        broker: Broker = PaperBroker(feed)
        if cfg.is_live:
            log.warning("Broker: PAPER — `broker: paper` overrides mode: live, "
                        "so NO real orders will be placed.")
        else:
            log.info("Broker: PAPER (no real orders will be placed)")
        return feed, broker

    # LIVE: build the matching live broker(s). We support a single asset class
    # of live trading per process for clarity; mixed live is left as an exercise.
    if classes == {AssetClass.CRYPTO} and crypto_client is not None:
        from .brokers.rh_crypto import RobinhoodCryptoBroker
        log.warning("Broker: LIVE CRYPTO — REAL orders will be placed")
        return feed, RobinhoodCryptoBroker(crypto_client)

    if classes == {AssetClass.STOCK} and have_stock_creds:
        from .brokers.rh_stock import RobinhoodStockBroker
        from .data.rh_stock_feed import RobinhoodStockFeed
        stock_feed = feeds[AssetClass.STOCK]
        assert isinstance(stock_feed, RobinhoodStockFeed)
        log.warning("Broker: LIVE STOCK — REAL orders will be placed")
        return feed, RobinhoodStockBroker(stock_feed)

    raise SystemExit(
        "LIVE mode currently supports a watchlist of a single asset class "
        "(all stock OR all crypto). Split into two processes to run both live."
    )


def _build_alpaca(cfg: Config,
                  symbol_class: Dict[str, AssetClass]) -> Tuple[DataFeed, Broker]:
    """One client serves data and orders for both stocks and crypto."""
    key, secret = cfg.secrets.alpaca_pair(cfg.is_live)
    if not (key and secret):
        which = ("ALPACA_LIVE_API_KEY / ALPACA_LIVE_API_SECRET" if cfg.is_live
                 else "ALPACA_API_KEY / ALPACA_API_SECRET")
        raise SystemExit(f"broker: alpaca selected but {which} are missing "
                         f"from .env.")

    from .alpaca_client import AlpacaClient
    from .brokers.alpaca import AlpacaBroker
    from .data.alpaca_feed import AlpacaFeed

    client = AlpacaClient(
        api_key=key, api_secret=secret,
        paper=not cfg.is_live,
        data_feed=cfg.alpaca_data_feed,
    )
    feed = AlpacaFeed(client, symbol_class, interval=cfg.bar_interval)
    broker = AlpacaBroker(client, symbol_class)

    if cfg.is_live:
        log.warning("Broker: ALPACA LIVE — REAL orders with REAL money")
    else:
        log.info("Broker: ALPACA PAPER — real order engine, fake money. "
                 "Unlimited round trips; no PDT cap, no settlement.")

    _report_account(cfg, broker)
    return feed, broker


def _build_schwab(cfg: Config) -> Tuple[DataFeed, Broker]:
    """Schwab is live-money-only; config validation already enforced that."""
    from .brokers.schwab import SchwabBroker
    from .data.schwab_feed import SchwabFeed
    from .schwab_client import (REAUTH_WARNING_SECONDS, SchwabAuthError,
                                SchwabClient)

    try:
        client = SchwabClient(
            app_key=cfg.secrets.schwab_app_key,
            app_secret=cfg.secrets.schwab_app_secret,
        )
    except SchwabAuthError as e:
        raise SystemExit(str(e))

    if cfg.secrets.schwab_account_number:
        if not client.select_account(cfg.secrets.schwab_account_number):
            raise SystemExit(
                f"Schwab account {cfg.secrets.schwab_account_number} not found "
                f"on these credentials."
            )

    left = client.refresh_seconds_left()
    if left < REAUTH_WARNING_SECONDS:
        log.warning("Schwab refresh token expires in %.1f hours — run "
                    "`python schwab_login.py` before then or trading stops "
                    "mid-session.", left / 3600)
    else:
        log.info("Schwab token good for %.1f more days.", left / 86400)

    feed = SchwabFeed(client, interval=cfg.bar_interval)
    broker = SchwabBroker(client)
    log.warning("Broker: SCHWAB LIVE — REAL orders with REAL money. "
                "This API has no paper mode.")

    _report_account(cfg, broker)
    return feed, broker


def _report_account(cfg: Config, broker) -> None:
    """Log what the broker says about the account, relaxing the PDT guard if moot.

    The local day-trade counter only sees trades this bot placed. The broker
    sees everything, so where the two disagree the broker wins.
    """
    acct: Optional[dict] = broker.account_snapshot()
    if not acct:
        log.warning("Could not read the %s account — continuing with the "
                    "local day-trade estimate.", broker.name)
        return

    if acct["blocked"]:
        raise SystemExit(
            f"{broker.name} account is restricted from trading "
            f"(status={acct['status']}). Resolve it with the broker first."
        )

    log.info("%s account: equity=$%.2f day_trades_used=%d pdt_flagged=%s",
             broker.name, acct["equity"], acct["daytrade_count"],
             acct["pattern_day_trader"])

    if acct["equity"] >= PDT_EQUITY_THRESHOLD and cfg.risk.pdt_guard:
        cfg.risk.pdt_guard = False
        log.info("Equity is above $%.0f — the PDT cap does not apply, "
                 "disabling the day-trade guard.", PDT_EQUITY_THRESHOLD)
    elif cfg.risk.pdt_guard:
        remaining = max(0, cfg.risk.max_day_trades_per_5_days
                        - acct["daytrade_count"])
        log.info("PDT guard ON: broker reports %d day trades used, %d left. "
                 "Crypto is exempt and always tradeable.",
                 acct["daytrade_count"], remaining)


def seed_live_book_from_broker(cfg, broker, state_file: str) -> bool:
    """Create a LIVE book from the broker when none exists yet.

    A fresh deployment (new server, new volume) has no state file, so
    `Portfolio.load_or_new` would invent one holding `paper_starting_cash` —
    $10,000 by default. On a live config that is a fabricated balance, and the
    cash guard rightly refuses to start on it. Adopting the broker's real cash
    and positions instead makes a first deploy work without a manual step.

    Returns True if a book was written.
    """
    if not cfg.is_live or os.path.exists(state_file):
        return False
    details = getattr(broker, "position_details", None)
    snap = getattr(broker, "account_snapshot", None)
    if details is None or snap is None:
        return False

    acct = snap() or {}
    try:
        cash = float(acct.get("cash") or 0)
    except (TypeError, ValueError):
        return False
    if cash <= 0 and not details():
        return False

    positions = details()
    book = {
        "starting_cash": cash + sum(
            p["quantity"] * p["avg_price"] for p in positions.values()),
        "cash": cash,
        "realized_pnl": 0.0,
        "day": market_date(),
        "day_start_equity": cash + sum(
            p["quantity"] * p["avg_price"] for p in positions.values()),
        "positions": {
            sym: {"symbol": sym, "asset_class": p["asset_class"].value,
                  "quantity": p["quantity"], "avg_price": p["avg_price"],
                  "last_buy_date": None}
            for sym, p in positions.items()
        },
        "day_trade_dates": [],
        "fills": [],
    }
    directory = os.path.dirname(state_file)
    if directory:
        os.makedirs(directory, exist_ok=True)
    with open(state_file, "w") as f:
        json.dump(book, f, indent=2)
    log.warning("No local book at %s — seeded it from %s: cash $%.2f, "
                "%d position(s). Realised P&L starts from zero.",
                state_file, broker.name, cash, len(positions))
    return True


def verify_cash_matches_broker(broker, portfolio, cfg,
                               tolerance: float = 1.0) -> None:
    """A LIVE book must start from the broker's real cash, never a default.

    `Portfolio.load_or_new` seeds a fresh book with `paper_starting_cash`,
    which defaults to $10,000. On a live config that silently hands the risk
    manager an account 33x bigger than the real one: the cash check passes on
    orders there is no money for, and every equity figure on the dashboard is
    fiction. Refuse to start rather than trade against an imaginary balance.
    """
    if not cfg.is_live:
        return
    snap = getattr(broker, "account_snapshot", None)
    if snap is None:
        return
    acct = snap() or {}
    try:
        broker_cash = float(acct.get("cash") or acct.get("buying_power") or 0)
    except (TypeError, ValueError):
        return
    if broker_cash <= 0:
        return
    if abs(broker_cash - portfolio.cash) > tolerance:
        raise SystemExit(
            f"LIVE cash mismatch: local book says ${portfolio.cash:,.2f}, "
            f"broker says ${broker_cash:,.2f}.\n"
            f"The risk manager would size orders against the wrong balance. "
            f"Stop the engine and run:\n"
            f"    .venv/bin/python reconcile.py --config <your live config> --apply"
        )


def warn_on_position_drift(broker, portfolio, tolerance: float = 1e-6) -> bool:
    """Compare local positions against the broker's. Returns True if they differ.

    The engine sizes exits from the LOCAL book, so if the two disagree it will
    either try to sell something it doesn't hold or leave a real position
    stranded. Any broker that can report its own positions gets checked; the
    rest are skipped silently.
    """
    held = getattr(broker, "held_quantities", None)
    if held is None:
        return False

    remote = held()
    local = {p.symbol: p.quantity for p in portfolio.open_positions()}
    drifted = False

    for symbol in sorted(set(remote) | set(local)):
        r, l = remote.get(symbol, 0.0), local.get(symbol, 0.0)
        if abs(r - l) > tolerance:
            drifted = True
            log.error("POSITION DRIFT %s: broker holds %.9f, local book has "
                      "%.9f", symbol, r, l)

    if drifted:
        log.error("The local portfolio disagrees with %s. Reconcile before "
                  "trading: edit state/portfolio.json, or flatten at the broker "
                  "and delete it to start clean.", broker.name)
    else:
        log.info("Position check OK — local book matches %s.", broker.name)
    return drifted


def _login_stock(cfg: Config) -> None:
    import robin_stocks.robinhood as rh
    mfa = None
    if cfg.secrets.rh_mfa_secret:
        import pyotp  # optional; only needed for app-based MFA
        mfa = pyotp.TOTP(cfg.secrets.rh_mfa_secret).now()
    log.info("Logging in to Robinhood (stock) as %s", cfg.secrets.rh_username)
    rh.authentication.login(
        username=cfg.secrets.rh_username,
        password=cfg.secrets.rh_password,
        mfa_code=mfa,
    )
