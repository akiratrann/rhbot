"""Core data types shared across the engine."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Optional


class AssetClass(str, Enum):
    STOCK = "stock"
    CRYPTO = "crypto"


class Side(str, Enum):
    BUY = "buy"
    SELL = "sell"


class SignalType(str, Enum):
    ENTER_LONG = "enter_long"   # open / add a long position
    EXIT_LONG = "exit_long"     # close a long position
    HOLD = "hold"               # do nothing


@dataclass(frozen=True)
class Bar:
    """One OHLCV candle."""
    ts: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass(frozen=True)
class Signal:
    """A strategy's decision for one symbol on one evaluation."""
    symbol: str
    type: SignalType
    reason: str = ""


@dataclass(frozen=True)
class Order:
    symbol: str
    asset_class: AssetClass
    side: Side
    notional: float                     # dollar amount to trade
    ts: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass
class Fill:
    symbol: str
    asset_class: AssetClass
    side: Side
    quantity: float
    price: float
    ts: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    @property
    def notional(self) -> float:
        return self.quantity * self.price


@dataclass
class Position:
    symbol: str
    asset_class: AssetClass
    quantity: float = 0.0
    avg_price: float = 0.0              # average cost basis
    #: US/Eastern date (YYYY-MM-DD) of the most recent BUY. Selling on this same
    #: date makes the round trip a "day trade" under the PDT rule.
    last_buy_date: Optional[str] = None
    #: ISO timestamp of the fill that opened this position from flat. Needed
    #: for a time-based exit: `last_buy_date` only records the most recent BUY,
    #: so a position added to would keep resetting its own age and never hit a
    #: holding limit.
    opened_ts: Optional[str] = None

    def market_value(self, last_price: float) -> float:
        return self.quantity * last_price

    def unrealized_pnl(self, last_price: float) -> float:
        return (last_price - self.avg_price) * self.quantity
