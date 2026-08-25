"""Configuration loading and validation."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import List, Optional

import yaml
from dotenv import load_dotenv

from .models import AssetClass


@dataclass
class WatchItem:
    symbol: str
    asset_class: AssetClass
    strategy: str
    params: dict = field(default_factory=dict)
    order_notional: float = 100.0


@dataclass
class RiskConfig:
    max_order_notional: float = 2000.0
    max_total_exposure: float = 8000.0
    max_open_positions: int = 5
    max_daily_loss: float = -500.0
    kill_switch_file: str = "state/STOP"
    # Minimum seconds between trades PER SYMBOL. This is your frequency governor:
    # it caps how often the slope strategy can flip, keeping you inside broker
    # limits (see README: PDT rule for stocks). 0 disables the cooldown.
    min_seconds_between_trades: int = 120
    # Pattern Day Trader guard. When on, blocks NEW STOCK ENTRIES once the
    # rolling day-trade budget is used up. Exits are never blocked (see risk.py).
    # Crypto is exempt from PDT and is never gated by this.
    pdt_guard: bool = True
    max_day_trades_per_5_days: int = 3


@dataclass
class DashboardConfig:
    enabled: bool = True
    host: str = "127.0.0.1"
    port: int = 5001


@dataclass
class Secrets:
    """Loaded from .env — never from config.yaml."""
    rh_username: Optional[str] = None
    rh_password: Optional[str] = None
    rh_mfa_secret: Optional[str] = None
    rh_crypto_api_key: Optional[str] = None
    rh_crypto_private_key: Optional[str] = None
    alpaca_api_key: Optional[str] = None
    alpaca_api_secret: Optional[str] = None
    # LIVE keys are a separate pair. Alpaca issues different credentials for
    # the live host, and keeping them in their own variables means switching a
    # config to `mode: live` cannot silently point a paper run at real money —
    # or break the paper run by overwriting its keys.
    alpaca_live_api_key: Optional[str] = None
    alpaca_live_api_secret: Optional[str] = None
    schwab_app_key: Optional[str] = None
    schwab_app_secret: Optional[str] = None
    schwab_callback_url: str = "https://127.0.0.1"
    schwab_account_number: Optional[str] = None

    @property
    def has_alpaca(self) -> bool:
        return bool(self.alpaca_api_key and self.alpaca_api_secret)

    @property
    def has_alpaca_live(self) -> bool:
        return bool(self.alpaca_live_api_key and self.alpaca_live_api_secret)

    def alpaca_pair(self, live: bool) -> tuple:
        """(key, secret) for the requested host. Never falls back across hosts."""
        if live:
            return self.alpaca_live_api_key, self.alpaca_live_api_secret
        return self.alpaca_api_key, self.alpaca_api_secret

    @property
    def has_schwab(self) -> bool:
        return bool(self.schwab_app_key and self.schwab_app_secret)


#: Valid values for the top-level `broker` key.
#:   auto      — Alpaca if its keys are present, else Robinhood/paper (default)
#:   alpaca    — Alpaca REST API (paper host in paper mode, live host in live)
#:   schwab    — Charles Schwab Trader API. LIVE ONLY: there is no paper
#:               account behind this API, so `mode: paper` is refused.
#:   robinhood — robin_stocks for stock + official Robinhood Crypto API
#:   paper     — force the built-in simulator even if credentials exist
BROKERS = ("auto", "alpaca", "schwab", "robinhood", "paper")


#: Yahoo interval -> (history range to request, default max bar age in minutes).
#: Max age is the staleness guard: if the newest bar is older than this, the
#: market is probably closed (or the feed is broken) and we must not trade.
#: Daily bars get 4 days of slack to survive a weekend plus a holiday.
BAR_INTERVALS = {
    "1m": ("5d", 15),
    "5m": ("1mo", 60),
    "15m": ("1mo", 120),
    "1h": ("3mo", 240),
    "1d": ("2y", 5760),
}


@dataclass
class Config:
    mode: str = "paper"
    #: Which venue to route orders to. See BROKERS above.
    broker: str = "auto"
    #: Alpaca stock data feed: "iex" (free, real-time, single venue) or "sip"
    #: (full consolidated tape, paid subscription). Ignored for crypto.
    alpaca_data_feed: str = "iex"
    paper_starting_cash: float = 10000.0
    poll_interval_seconds: int = 60
    #: Bar size strategies see. MUST match what you tuned params on in sweep.py.
    bar_interval: str = "1d"
    #: Refuse to trade if the newest bar is older than this. None = auto.
    max_bar_age_minutes: Optional[int] = None
    watchlist: List[WatchItem] = field(default_factory=list)
    risk: RiskConfig = field(default_factory=RiskConfig)
    dashboard: DashboardConfig = field(default_factory=DashboardConfig)
    #: Where the local book lives. MUST differ per account — a live config
    #: sharing the paper book would boot holding positions and cash that do
    #: not exist at the live broker, and size real orders against them.
    state_file: str = "state/portfolio.json"
    log_level: str = "INFO"
    log_file: str = "logs/rhbot.log"
    secrets: Secrets = field(default_factory=Secrets)

    @property
    def is_live(self) -> bool:
        return self.mode.lower() == "live"

    @property
    def yahoo_range(self) -> str:
        return BAR_INTERVALS[self.bar_interval][0]

    @property
    def bar_age_limit_minutes(self) -> int:
        """Resolved staleness threshold (explicit override, else per-interval)."""
        if self.max_bar_age_minutes is not None:
            return self.max_bar_age_minutes
        return BAR_INTERVALS[self.bar_interval][1]


def load_config(path: str = "config.yaml") -> Config:
    load_dotenv()  # populate os.environ from .env if present

    with open(path, "r") as f:
        raw = yaml.safe_load(f) or {}

    watch = []
    for w in raw.get("watchlist", []):
        watch.append(
            WatchItem(
                symbol=w["symbol"],
                asset_class=AssetClass(w.get("asset_class", "stock")),
                strategy=w["strategy"],
                params=w.get("params", {}) or {},
                order_notional=float(w.get("order_notional", 100.0)),
            )
        )

    r = raw.get("risk", {}) or {}
    d = raw.get("dashboard", {}) or {}
    lg = raw.get("logging", {}) or {}

    cfg = Config(
        mode=str(raw.get("mode", "paper")),
        broker=str(raw.get("broker", "auto")).lower(),
        alpaca_data_feed=str(raw.get("alpaca_data_feed", "iex")).lower(),
        paper_starting_cash=float(raw.get("paper_starting_cash", 10000.0)),
        poll_interval_seconds=int(raw.get("poll_interval_seconds", 60)),
        bar_interval=str(raw.get("bar_interval", "1d")),
        max_bar_age_minutes=(
            int(raw["max_bar_age_minutes"])
            if raw.get("max_bar_age_minutes") is not None else None
        ),
        watchlist=watch,
        risk=RiskConfig(
            max_order_notional=float(r.get("max_order_notional", 2000.0)),
            max_total_exposure=float(r.get("max_total_exposure", 8000.0)),
            max_open_positions=int(r.get("max_open_positions", 5)),
            max_daily_loss=float(r.get("max_daily_loss", -500.0)),
            kill_switch_file=str(r.get("kill_switch_file", "state/STOP")),
            min_seconds_between_trades=int(
                r.get("min_seconds_between_trades", 120)
            ),
            pdt_guard=bool(r.get("pdt_guard", True)),
            max_day_trades_per_5_days=int(
                r.get("max_day_trades_per_5_days", 3)
            ),
        ),
        dashboard=DashboardConfig(
            enabled=bool(d.get("enabled", True)),
            host=str(d.get("host", "127.0.0.1")),
            port=int(d.get("port", 5001)),
        ),
        state_file=str(raw.get("state_file", "state/portfolio.json")),
        log_level=str(lg.get("level", "INFO")),
        log_file=str(lg.get("file", "logs/rhbot.log")),
        secrets=Secrets(
            rh_username=os.getenv("RH_USERNAME") or None,
            rh_password=os.getenv("RH_PASSWORD") or None,
            rh_mfa_secret=os.getenv("RH_MFA_SECRET") or None,
            rh_crypto_api_key=os.getenv("RH_CRYPTO_API_KEY") or None,
            rh_crypto_private_key=os.getenv("RH_CRYPTO_PRIVATE_KEY") or None,
            alpaca_api_key=os.getenv("ALPACA_API_KEY") or None,
            alpaca_api_secret=os.getenv("ALPACA_API_SECRET") or None,
            alpaca_live_api_key=os.getenv("ALPACA_LIVE_API_KEY") or None,
            alpaca_live_api_secret=os.getenv("ALPACA_LIVE_API_SECRET") or None,
            schwab_app_key=os.getenv("SCHWAB_APP_KEY") or None,
            schwab_app_secret=os.getenv("SCHWAB_APP_SECRET") or None,
            schwab_callback_url=(os.getenv("SCHWAB_CALLBACK_URL")
                                 or "https://127.0.0.1"),
            schwab_account_number=os.getenv("SCHWAB_ACCOUNT_NUMBER") or None,
        ),
    )
    _validate(cfg)
    return cfg


def _validate(cfg: Config) -> None:
    if cfg.mode.lower() not in ("paper", "live"):
        raise ValueError(f"mode must be 'paper' or 'live', got {cfg.mode!r}")
    if cfg.broker not in BROKERS:
        raise ValueError(
            f"broker must be one of {list(BROKERS)}, got {cfg.broker!r}"
        )
    if cfg.alpaca_data_feed not in ("iex", "sip"):
        raise ValueError(
            f"alpaca_data_feed must be 'iex' or 'sip', got "
            f"{cfg.alpaca_data_feed!r}"
        )
    if cfg.broker == "alpaca":
        if cfg.is_live and not cfg.secrets.has_alpaca_live:
            raise ValueError(
                "mode: live with broker: alpaca requires ALPACA_LIVE_API_KEY "
                "and ALPACA_LIVE_API_SECRET in .env. Paper keys do NOT work "
                "against the live host — generate live keys from the Alpaca "
                "dashboard with the account switcher set to Live."
            )
        if not cfg.is_live and not cfg.secrets.has_alpaca:
            raise ValueError(
                "broker: alpaca requires ALPACA_API_KEY and ALPACA_API_SECRET "
                "in .env (get free paper keys at https://alpaca.markets)"
            )
    if cfg.broker == "schwab":
        if not cfg.secrets.has_schwab:
            raise ValueError(
                "broker: schwab requires SCHWAB_APP_KEY and SCHWAB_APP_SECRET "
                "in .env (register a Trader API app at "
                "https://developer.schwab.com)"
            )
        # The Schwab API has no paper account behind it. Silently placing real
        # orders while the config says `paper` would be the worst possible
        # failure mode, so refuse the combination outright.
        if not cfg.is_live:
            raise ValueError(
                "broker: schwab has no paper account — the Schwab Trader API "
                "trades live money only. Set `mode: live` to confirm you mean "
                "it, or use `broker: alpaca` with `mode: paper` for testing."
            )
        crypto = sorted({w.symbol for w in cfg.watchlist
                         if w.asset_class == AssetClass.CRYPTO})
        if crypto:
            raise ValueError(
                f"broker: schwab cannot trade crypto ({', '.join(crypto)}). "
                f"Remove those symbols, or run crypto in a separate process "
                f"with `broker: alpaca`."
            )
    if cfg.poll_interval_seconds < 1:
        raise ValueError("poll_interval_seconds must be >= 1")
    if cfg.bar_interval not in BAR_INTERVALS:
        raise ValueError(
            f"bar_interval must be one of {sorted(BAR_INTERVALS)}, "
            f"got {cfg.bar_interval!r}"
        )
    if not cfg.watchlist:
        raise ValueError("watchlist is empty — nothing to trade")
    if cfg.is_live and cfg.state_file == "state/portfolio.json":
        raise ValueError(
            "a LIVE config must set its own `state_file` (e.g. "
            "state/portfolio-live.json). Sharing the default with a paper run "
            "would load the paper book — wrong cash, wrong positions — and "
            "size real orders against it."
        )
    # Fail fast if any per-symbol order exceeds the hard cap.
    for w in cfg.watchlist:
        if w.order_notional > cfg.risk.max_order_notional:
            raise ValueError(
                f"{w.symbol}: order_notional {w.order_notional} exceeds "
                f"risk.max_order_notional {cfg.risk.max_order_notional}"
            )
