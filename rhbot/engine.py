"""The trading engine — the loop that runs unattended.

Each tick, for every symbol:
  1. pull recent bars + latest price
  2. ask the strategy for a Signal
  3. turn an actionable Signal into an Order
  4. run the Order through the RiskManager
  5. if allowed, submit to the broker and record the fill

Global halts (kill switch, daily-loss cap) are checked once per tick before any
orders. Everything is wrapped so one bad symbol never takes down the loop.
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timezone
from typing import Dict, List, Optional

from .brokers.base import Broker
from .config import Config
from .data.feed import DataFeed
from .data.rh_crypto_feed import RobinhoodCryptoFeed
from .models import AssetClass, Order, Side, Signal, SignalType
from .portfolio import Portfolio
from .risk import RiskManager
from .strategy import build_strategy

log = logging.getLogger("rhbot.engine")


class Engine:
    def __init__(self, cfg: Config, feed: DataFeed, broker: Broker,
                 portfolio: Portfolio, risk: RiskManager):
        self.cfg = cfg
        self.feed = feed
        self.broker = broker
        self.portfolio = portfolio
        self.risk = risk

        # Build one strategy instance per watch item.
        self.strategies = {
            w.symbol: build_strategy(w.strategy, w.params) for w in cfg.watchlist
        }
        self.watch = {w.symbol: w for w in cfg.watchlist}

        #: Seconds to wait after a no-fill before retrying that symbol. Long
        #: enough that a closed session doesn't generate a tick's worth of
        #: orders, short enough to resume promptly once it reopens.
        self.retry_backoff_seconds = max(300, cfg.poll_interval_seconds * 5)

        self._stop = threading.Event()
        self.last_prices: Dict[str, float] = {}
        self.last_tick_ts: Optional[float] = None
        self.tick_count = 0
        # Frequency governor: last trade time per symbol (monotonic seconds).
        self._last_trade_ts: Dict[str, float] = {}
        # Backoff after a submission that produced no fill. Without this, a
        # persistent reject (session closed, no buying power, bad symbol)
        # re-signals and re-submits every single tick.
        self._retry_after: Dict[str, float] = {}
        # Exit-only strategy for held symbols that have left the watchlist.
        self.orphan_strategy = "slope_reversal"
        self.orphan_params: Dict = {"smooth": 3, "min_slope_pct": 0.002}
        self._orphan_cache: Dict[str, tuple] = {}
        # Throttles repeated "stale data" logging to roughly once per hour.
        self._stale_logged: Dict[str, int] = {}

    # ---- lifecycle --------------------------------------------------------

    def stop(self) -> None:
        self._stop.set()

    def run_forever(self) -> None:
        log.info("Engine starting: mode=%s broker=%s symbols=%s",
                 self.cfg.mode, self.broker.name, list(self.watch))
        try:
            while not self._stop.is_set():
                try:
                    self.tick()
                except Exception:  # noqa: BLE001 — loop must survive anything
                    log.exception("tick failed")
                self._stop.wait(self.cfg.poll_interval_seconds)
        finally:
            self.portfolio.save()
            log.info("Engine stopped. Final equity ~ %.2f",
                     self.portfolio.equity(self.last_prices))

    # ---- one iteration ----------------------------------------------------

    def tick(self) -> None:
        self.tick_count += 1
        self.last_tick_ts = time.time()

        # Refresh prices first so risk/global checks use current marks.
        prices = self._refresh_prices()

        if self.risk.check_global_halts(self.portfolio, prices):
            self.portfolio.save()
            return

        # Evaluate the watchlist AND anything still held. A position dropped
        # from the watchlist (by an edit, or by relearn.py rewriting it) is not
        # dropped from the account — without this it can never be sold, because
        # nothing ever asks its strategy for an exit.
        held = {p.symbol for p in self.portfolio.open_positions()}
        for symbol in list(self.watch) + sorted(held - set(self.watch)):
            try:
                self._process_symbol(symbol, prices)
            except Exception:  # noqa: BLE001
                log.exception("processing %s failed", symbol)

        self.portfolio.save()

    def _refresh_prices(self) -> Dict[str, float]:
        # Price the watchlist AND anything still held. A position dropped from
        # the watchlist is not dropped from the account: without this it marks
        # at cost forever, silently overstating equity and feeding a wrong
        # number to the daily-loss kill switch.
        held = {p.symbol for p in self.portfolio.open_positions()}
        for symbol in list(self.watch) + sorted(held - set(self.watch)):
            price = self.feed.get_price(symbol)
            if price is not None:
                self.last_prices[symbol] = price
                # Crypto feed builds its own history from observed ticks.
                if isinstance(self.feed, RobinhoodCryptoFeed):
                    self.feed.record(symbol, price, datetime.now(timezone.utc))
        return self.last_prices

    def _is_stale(self, symbol: str, bars: List) -> bool:
        """True if the newest bar is too old to trade on.

        Guards against acting on a closed market (weekends/holidays/after hours)
        or a silently broken feed — both of which otherwise look like a flat
        price line that a strategy will happily misread.
        """
        if not bars:
            return True
        age_min = (datetime.now(timezone.utc) - bars[-1].ts).total_seconds() / 60
        # Per-symbol: a 15m symbol judged against the 1d threshold (4 days)
        # would happily trade on bars from last week.
        limit = self.cfg.bar_age_limit_for(symbol)
        if age_min > limit:
            if self._stale_logged.get(symbol) != int(age_min // 60):
                log.info("STALE %s: newest bar is %.0f min old (limit %d) — "
                         "market likely closed, not trading",
                         symbol, age_min, limit)
                self._stale_logged[symbol] = int(age_min // 60)
            return True
        self._stale_logged.pop(symbol, None)
        return False

    @staticmethod
    def _position_age_days(position) -> Optional[float]:
        """Days since the position was OPENED, or None if unknown.

        None on unparseable/missing stamps rather than 0: a missing timestamp
        must never read as "infinitely old" and force an unwanted exit.
        """
        raw = getattr(position, "opened_ts", None)
        if not raw:
            return None
        try:
            opened = datetime.fromisoformat(raw)
        except (TypeError, ValueError):
            return None
        if opened.tzinfo is None:
            opened = opened.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - opened).total_seconds() / 86400.0

    def _orphan(self, symbol: str):
        """Strategy + watch item for a held symbol that left the watchlist.

        Built lazily from the last config that mentioned it, falling back to a
        default. Entries are refused for these (see `_process_symbol`) — the
        only reason to keep evaluating them is so they can be EXITED.
        """
        if symbol not in self._orphan_cache:
            pos = self.portfolio.positions.get(symbol)
            asset_class = pos.asset_class if pos else AssetClass.STOCK
            self._orphan_cache[symbol] = (
                build_strategy(self.orphan_strategy, self.orphan_params),
                asset_class,
            )
            log.warning("%s is held but no longer on the watchlist — "
                        "evaluating it for EXITS ONLY so it can be closed.",
                        symbol)
        return self._orphan_cache[symbol]

    def _process_symbol(self, symbol: str, prices: Dict[str, float]) -> None:
        orphaned = symbol not in self.watch
        if orphaned:
            strat, asset_class = self._orphan(symbol)
            item = None
        else:
            item = self.watch[symbol]
            strat = self.strategies[symbol]
            asset_class = item.asset_class
        bars = self.feed.get_bars(symbol, strat.warmup_bars + 5)

        if self._is_stale(symbol, bars):
            return
        position = self.portfolio.positions.get(symbol)
        if position is not None and position.quantity <= 0:
            position = None

        signal: Signal = strat.evaluate(symbol, bars, position)

        # Time-based exit, overriding the strategy. Measured out of sample: a
        # 2-day cap trades ~78 round trips vs ~51 uncapped and wins 46% vs 41%,
        # for less total return — it takes small wins and lets losers reach the
        # cap. Deliberate trade: more activity, lower expectancy per trade.
        # An overnight hold is not a day trade, so this stays PDT-safe.
        limit = None if item is None else item.max_hold_days
        if (limit and position is not None and position.quantity > 0
                and signal.type != SignalType.EXIT_LONG):
            age = self._position_age_days(position)
            if age is not None and age >= limit:
                signal = Signal(symbol, SignalType.EXIT_LONG,
                                f"max hold {limit:g}d reached ({age:.1f}d)")

        if signal.type == SignalType.HOLD:
            return

        side = Side.BUY if signal.type == SignalType.ENTER_LONG else Side.SELL
        if orphaned and side == Side.BUY:
            # Never open a NEW position in something the config no longer lists.
            return
        if side == Side.SELL:
            # Sell the full position (dollar value at current mark).
            price = prices.get(symbol)
            if not position or not price:
                return
            notional = position.quantity * price
        else:
            notional = item.order_notional  # entries only ever reach here

        # Backoff from a previous failed submission.
        retry_at = self._retry_after.get(symbol)
        if retry_at is not None and time.time() < retry_at:
            return

        # Frequency governor — enforce the per-symbol cooldown.
        cooldown = self.cfg.risk.min_seconds_between_trades
        last = self._last_trade_ts.get(symbol)
        if cooldown > 0 and last is not None and (time.time() - last) < cooldown:
            wait = cooldown - (time.time() - last)
            log.info("COOLDOWN %s %s: %.0fs left", side.value.upper(), symbol, wait)
            return

        order = Order(symbol=symbol, asset_class=asset_class,
                      side=side, notional=notional)

        reason = self.risk.check(order, self.portfolio, prices)
        if reason:
            log.info("BLOCKED %s %s: %s", side.value.upper(), symbol, reason)
            return

        log.info("SIGNAL %s %s -> %s ($%.2f) | %s",
                 signal.type.value, symbol, side.value.upper(),
                 notional, signal.reason)

        fill = self.broker.submit(order)
        if fill:
            self.portfolio.apply_fill(fill)
            self._last_trade_ts[symbol] = time.time()
            self._retry_after.pop(symbol, None)
        else:
            # No fill: back off before trying again. The cooldown above only
            # advances on SUCCESS, so without this a repeatable failure churns
            # an order at the broker on every tick.
            self._retry_after[symbol] = time.time() + self.retry_backoff_seconds
            log.info("NO FILL %s %s — backing off %ds",
                     side.value.upper(), symbol, self.retry_backoff_seconds)

    # ---- introspection for the dashboard ----------------------------------

    def snapshot(self) -> dict:
        prices = self.last_prices
        positions = []
        for p in self.portfolio.open_positions():
            last = prices.get(p.symbol, p.avg_price)
            positions.append({
                "symbol": p.symbol,
                "asset_class": p.asset_class.value,
                "quantity": p.quantity,
                "avg_price": p.avg_price,
                "last_price": last,
                "market_value": p.market_value(last),
                "unrealized_pnl": p.unrealized_pnl(last),
            })
        return {
            "mode": self.cfg.mode,
            "broker": self.broker.name,
            "is_live": self.broker.is_live(),
            "tick_count": self.tick_count,
            "last_tick_ts": self.last_tick_ts,
            "halted": self.risk.halted(),
            "halt_reason": self.risk.halt_reason(),
            "bar_interval": self.cfg.bar_interval,
            "day_trades_used": self.portfolio.day_trades_in_window(),
            "day_trades_limit": self.cfg.risk.max_day_trades_per_5_days,
            "cash": self.portfolio.cash,
            "equity": self.portfolio.equity(prices),
            "realized_pnl": self.portfolio.realized_pnl,
            "unrealized_pnl": self.portfolio.unrealized_pnl(prices),
            "daily_pnl": self.portfolio.daily_pnl(prices),
            "exposure": self.portfolio.exposure(prices),
            "positions": positions,
            "prices": dict(prices),
            "recent_fills": self.portfolio.fills[-20:][::-1],
        }
