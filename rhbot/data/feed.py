"""Data feed interface, a routing composite, and a credential-free synthetic feed.

The engine only ever talks to a `DataFeed`. Concrete feeds (Robinhood stock /
crypto) are plugged in by the factory in `rhbot.factory`. `SyntheticFeed` lets
you run the whole service end-to-end with no accounts or keys — ideal for a
first smoke test and for unit tests.
"""

from __future__ import annotations

import math
import random
from abc import ABC, abstractmethod
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

from ..models import AssetClass, Bar


class DataFeed(ABC):
    @abstractmethod
    def get_price(self, symbol: str) -> Optional[float]:
        """Latest price for `symbol`."""

    @abstractmethod
    def get_bars(self, symbol: str, count: int) -> List[Bar]:
        """Up to `count` most recent bars, oldest first."""


class CompositeFeed(DataFeed):
    """Routes each symbol to the feed registered for its asset class."""

    def __init__(self, by_class: Dict[AssetClass, DataFeed],
                 symbol_class: Dict[str, AssetClass]):
        self._by_class = by_class
        self._symbol_class = symbol_class

    def _feed_for(self, symbol: str) -> Optional[DataFeed]:
        ac = self._symbol_class.get(symbol)
        return self._by_class.get(ac) if ac else None

    def get_price(self, symbol: str) -> Optional[float]:
        feed = self._feed_for(symbol)
        return feed.get_price(symbol) if feed else None

    def get_bars(self, symbol: str, count: int) -> List[Bar]:
        feed = self._feed_for(symbol)
        return feed.get_bars(symbol, count) if feed else []


class SyntheticFeed(DataFeed):
    """Deterministic-per-symbol random walk. NOT real data — for testing only."""

    def __init__(self, seed: int = 42, base_prices: Optional[Dict[str, float]] = None):
        self._rng = random.Random(seed)
        self._base = base_prices or {}
        self._series: Dict[str, List[Bar]] = {}

    def _ensure(self, symbol: str, count: int) -> List[Bar]:
        series = self._series.get(symbol, [])
        if len(series) >= count:
            return series
        start = self._base.get(symbol, 100.0)
        price = series[-1].close if series else start
        now = datetime.now(timezone.utc)
        need = count - len(series)
        new: List[Bar] = []
        for i in range(need):
            drift = math.sin((len(series) + i) / 8.0) * 0.002
            shock = self._rng.gauss(0, 0.01)
            price = max(0.01, price * (1 + drift + shock))
            ts = now - timedelta(minutes=(need - i))
            o = price * (1 + self._rng.uniform(-0.002, 0.002))
            new.append(Bar(ts=ts, open=o, high=max(o, price),
                           low=min(o, price), close=price, volume=1000.0))
        series = series + new
        self._series[symbol] = series
        return series

    def get_bars(self, symbol: str, count: int) -> List[Bar]:
        return self._ensure(symbol, count)[-count:]

    def get_price(self, symbol: str) -> Optional[float]:
        bars = self._ensure(symbol, 1)
        return bars[-1].close if bars else None
