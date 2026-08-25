"""Crypto price feed backed by the official Robinhood Crypto API."""

from __future__ import annotations

import logging
from typing import List, Optional

from ..models import Bar
from ..rh_crypto_client import RobinhoodCryptoClient
from .feed import DataFeed

log = logging.getLogger("rhbot.crypto")


class RobinhoodCryptoFeed(DataFeed):
    """Live prices from Robinhood crypto. Bars are synthesized from a rolling
    buffer of observed prices (the public quote endpoint returns spot, not OHLC)."""

    def __init__(self, client: RobinhoodCryptoClient, buffer_size: int = 500):
        self.client = client
        self._buffers: dict[str, List[Bar]] = {}
        self._buffer_size = buffer_size

    def get_price(self, symbol: str) -> Optional[float]:
        return self.client.get_price(symbol)

    def record(self, symbol: str, price: float, ts) -> None:
        """Append an observed price as a 1-point bar. Called by the engine each
        tick so strategies have a price history to work with."""
        buf = self._buffers.setdefault(symbol, [])
        buf.append(Bar(ts=ts, open=price, high=price, low=price,
                       close=price, volume=0.0))
        if len(buf) > self._buffer_size:
            del buf[0]

    def get_bars(self, symbol: str, count: int) -> List[Bar]:
        return self._buffers.get(symbol, [])[-count:]
