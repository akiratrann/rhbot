"""Tracks cash, positions, and PnL. Persists to disk so restarts are safe."""

from __future__ import annotations

import json
import os
from dataclasses import asdict
from datetime import date, datetime, timedelta, timezone
from typing import Dict, List, Optional
from zoneinfo import ZoneInfo

from .models import AssetClass, Fill, Position, Side

#: PDT is a US market rule, so day boundaries follow the US market timezone.
MARKET_TZ = ZoneInfo("America/New_York")


def market_date(ts: Optional[datetime] = None) -> str:
    """Current (or given) date in US market time, as YYYY-MM-DD."""
    ts = ts or datetime.now(timezone.utc)
    return ts.astimezone(MARKET_TZ).strftime("%Y-%m-%d")


class Portfolio:
    def __init__(self, starting_cash: float, state_file: str = "state/portfolio.json"):
        self.starting_cash = starting_cash
        self.cash = starting_cash
        self.positions: Dict[str, Position] = {}
        self.realized_pnl = 0.0
        self.fills: List[dict] = []
        #: Market dates on which a day trade occurred (one entry per day trade).
        self.day_trade_dates: List[str] = []
        self.state_file = state_file
        self._day = _today()
        self._day_start_equity = starting_cash

    # ---- mutation ---------------------------------------------------------

    def apply_fill(self, fill: Fill) -> None:
        pos = self.positions.get(fill.symbol)
        if pos is None:
            pos = Position(symbol=fill.symbol, asset_class=fill.asset_class)
            self.positions[fill.symbol] = pos

        today = market_date(fill.ts)

        if fill.side == Side.BUY:
            new_qty = pos.quantity + fill.quantity
            # Weighted-average cost basis.
            if new_qty > 0:
                pos.avg_price = (
                    pos.avg_price * pos.quantity + fill.price * fill.quantity
                ) / new_qty
            pos.quantity = new_qty
            pos.last_buy_date = today
            self.cash -= fill.notional
        else:  # SELL
            # Selling something bought today = a day trade (PDT-countable).
            # Crypto is exempt from the rule, so it is never counted.
            if (pos.last_buy_date == today
                    and fill.asset_class != AssetClass.CRYPTO):
                self.day_trade_dates.append(today)
            self.realized_pnl += (fill.price - pos.avg_price) * fill.quantity
            pos.quantity -= fill.quantity
            self.cash += fill.notional
            if pos.quantity <= 1e-9:
                pos.quantity = 0.0
                pos.avg_price = 0.0
                pos.last_buy_date = None

        self.fills.append({**asdict(fill), "ts": fill.ts.isoformat(),
                           "asset_class": fill.asset_class.value,
                           "side": fill.side.value})

    # ---- reads ------------------------------------------------------------

    def open_positions(self) -> List[Position]:
        return [p for p in self.positions.values() if p.quantity > 0]

    def day_trades_in_window(self, business_days: int = 5) -> int:
        """Day trades within the trailing `business_days` window.

        Walks back over weekdays to find the window start, then counts recorded
        day trades on or after it. Market holidays are not modelled, so this is
        slightly CONSERVATIVE (it may count a marginally older trade) — the safe
        direction to err for a compliance guard.
        """
        cutoff = _business_days_ago(business_days - 1)
        return sum(1 for d in self.day_trade_dates if d >= cutoff)

    def exposure(self, prices: Dict[str, float]) -> float:
        return sum(
            p.market_value(prices.get(p.symbol, p.avg_price))
            for p in self.open_positions()
        )

    def equity(self, prices: Dict[str, float]) -> float:
        return self.cash + self.exposure(prices)

    def unrealized_pnl(self, prices: Dict[str, float]) -> float:
        return sum(
            p.unrealized_pnl(prices.get(p.symbol, p.avg_price))
            for p in self.open_positions()
        )

    def daily_pnl(self, prices: Dict[str, float]) -> float:
        """PnL since the start of the current calendar day (UTC)."""
        self._roll_day_if_needed(prices)
        return self.equity(prices) - self._day_start_equity

    def _roll_day_if_needed(self, prices: Dict[str, float]) -> None:
        today = _today()
        if today != self._day:
            self._day = today
            self._day_start_equity = self.equity(prices)

    # ---- persistence ------------------------------------------------------

    def save(self) -> None:
        os.makedirs(os.path.dirname(self.state_file) or ".", exist_ok=True)
        data = {
            "starting_cash": self.starting_cash,
            "cash": self.cash,
            "realized_pnl": self.realized_pnl,
            "day": self._day,
            "day_start_equity": self._day_start_equity,
            "positions": {
                s: {
                    "symbol": p.symbol,
                    "asset_class": p.asset_class.value,
                    "quantity": p.quantity,
                    "avg_price": p.avg_price,
                    "last_buy_date": p.last_buy_date,
                }
                for s, p in self.positions.items()
            },
            # Keep ~2 months so the 5-business-day window always has its data.
            "day_trade_dates": self.day_trade_dates[-200:],
            "fills": self.fills[-500:],  # keep the tail
        }
        tmp = self.state_file + ".tmp"
        with open(tmp, "w") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, self.state_file)

    @classmethod
    def load_or_new(cls, starting_cash: float,
                    state_file: str = "state/portfolio.json") -> "Portfolio":
        if not os.path.exists(state_file):
            return cls(starting_cash, state_file)
        with open(state_file) as f:
            data = json.load(f)
        p = cls(data.get("starting_cash", starting_cash), state_file)
        p.cash = data.get("cash", starting_cash)
        p.realized_pnl = data.get("realized_pnl", 0.0)
        p._day = data.get("day", _today())
        p._day_start_equity = data.get("day_start_equity", p.cash)
        p.fills = data.get("fills", [])
        p.day_trade_dates = data.get("day_trade_dates", [])
        for s, pd in data.get("positions", {}).items():
            p.positions[s] = Position(
                symbol=pd["symbol"],
                asset_class=AssetClass(pd["asset_class"]),
                quantity=pd["quantity"],
                avg_price=pd["avg_price"],
                last_buy_date=pd.get("last_buy_date"),
            )
        return p


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _business_days_ago(n: int, today: Optional[date] = None) -> str:
    """Date `n` business days back from today (market tz), as YYYY-MM-DD."""
    d = today or datetime.now(timezone.utc).astimezone(MARKET_TZ).date()
    stepped = 0
    while stepped < n:
        d -= timedelta(days=1)
        if d.weekday() < 5:  # Mon-Fri
            stepped += 1
    return d.strftime("%Y-%m-%d")
