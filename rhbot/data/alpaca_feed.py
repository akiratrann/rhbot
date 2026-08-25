"""Market data from Alpaca — official, keyed, and covers stock + crypto.

Unlike `YahooFeed` (unofficial scrape, no SLA) this is a supported endpoint you
are authenticated against, so it is safe to poll and safe to trade on. It needs
the symbol -> asset class map because Alpaca serves stocks and crypto from
different API versions with different symbol formats.

Free-tier caveat: stock bars come from IEX, not the full SIP consolidated tape.
Prices are real-time but from a single venue, so thin names can look jumpy.
Crypto is unaffected. Set `data_feed: sip` in config if you pay for it.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

from ..alpaca_client import TIMEFRAMES, AlpacaClient
from ..models import AssetClass, Bar
from .feed import DataFeed

log = logging.getLogger("rhbot.alpaca")


class AlpacaFeed(DataFeed):
    def __init__(self, client: AlpacaClient,
                 symbol_class: Dict[str, AssetClass],
                 interval: str = "1d", cache_seconds: int = 30,
                 symbol_interval: Optional[Dict[str, str]] = None):
        """`symbol_interval` overrides `interval` per symbol.

        Crypto is PDT-exempt and can trade intraday while equities at a small
        account size cannot, so one feed has to serve both bar sizes at once.
        """
        self.client = client
        self.symbol_class = symbol_class
        self.default_timeframe = TIMEFRAMES[interval]
        self.symbol_timeframe = {
            sym: TIMEFRAMES[iv] for sym, iv in (symbol_interval or {}).items()
        }
        self.cache_seconds = cache_seconds
        self._cache: Dict[str, Tuple[float, List[Bar]]] = {}

    def timeframe_for(self, symbol: str) -> str:
        return self.symbol_timeframe.get(symbol, self.default_timeframe)

    def _is_crypto(self, symbol: str) -> bool:
        return self.symbol_class.get(symbol) == AssetClass.CRYPTO

    def get_price(self, symbol: str) -> Optional[float]:
        return self.client.get_price(symbol, self._is_crypto(symbol))

    def get_bars(self, symbol: str, count: int) -> List[Bar]:
        now = time.time()
        hit = self._cache.get(symbol)
        if hit and (now - hit[0]) < self.cache_seconds and len(hit[1]) >= count:
            return hit[1][-count:]

        # Over-fetch a little so a cache entry can serve slightly larger asks.
        raw = self.client.get_bars(symbol, self.timeframe_for(symbol),
                                   max(count, 100), self._is_crypto(symbol))
        bars: List[Bar] = []
        for b in raw:
            try:
                bars.append(Bar(
                    ts=datetime.fromisoformat(
                        b["t"].replace("Z", "+00:00")
                    ).astimezone(timezone.utc),
                    open=float(b["o"]),
                    high=float(b["h"]),
                    low=float(b["l"]),
                    close=float(b["c"]),
                    volume=float(b.get("v", 0) or 0),
                ))
            except (KeyError, TypeError, ValueError):
                continue

        if not bars:
            # Serve stale bars rather than an empty list: the engine reads an
            # empty list as "no data" and the staleness guard already refuses
            # to trade on bars that are too old, so this cannot trade on junk.
            log.warning("alpaca: no bars for %s (%s)", symbol,
                        self.timeframe_for(symbol))
            return hit[1][-count:] if hit else []

        self._cache[symbol] = (now, bars)
        return bars[-count:]
