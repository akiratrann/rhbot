"""Market data from the official Schwab Trader API.

Real-time consolidated quotes come with the brokerage account, so this is
better data than Alpaca's free IEX tier. Two shape differences to know about:

  * Schwab has no native hourly candle. `1h` is aggregated here from 30-minute
    candles rather than silently returning the wrong bar size.
  * There is no crypto on this API at all. The factory refuses to start with a
    crypto symbol on the Schwab path.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

from ..models import Bar
from ..schwab_client import SchwabClient
from .feed import DataFeed

log = logging.getLogger("rhbot.schwab")


class SchwabFeed(DataFeed):
    def __init__(self, client: SchwabClient, interval: str = "1d",
                 cache_seconds: int = 30):
        self.client = client
        self.interval = interval
        self.cache_seconds = cache_seconds
        self._cache: Dict[str, Tuple[float, List[Bar]]] = {}

    def get_price(self, symbol: str) -> Optional[float]:
        return self.client.get_quote(symbol)

    def get_bars(self, symbol: str, count: int) -> List[Bar]:
        now = time.time()
        hit = self._cache.get(symbol)
        if hit and (now - hit[0]) < self.cache_seconds and len(hit[1]) >= count:
            return hit[1][-count:]

        candles = self.client.get_candles(symbol, self.interval)
        bars: List[Bar] = []
        for c in candles:
            try:
                bars.append(Bar(
                    ts=datetime.fromtimestamp(
                        float(c["datetime"]) / 1000.0, tz=timezone.utc),
                    open=float(c["open"]),
                    high=float(c["high"]),
                    low=float(c["low"]),
                    close=float(c["close"]),
                    volume=float(c.get("volume", 0) or 0),
                ))
            except (KeyError, TypeError, ValueError):
                continue

        if self.interval == "1h":
            bars = _pair_into_hours(bars)

        if not bars:
            # Serve stale bars rather than an empty list. The engine reads empty
            # as "no data", and the staleness guard already refuses to trade on
            # bars that are too old, so this cannot trade on junk.
            log.warning("schwab: no candles for %s (%s)", symbol, self.interval)
            return hit[1][-count:] if hit else []

        self._cache[symbol] = (now, bars)
        return bars[-count:]


def _pair_into_hours(bars: List[Bar]) -> List[Bar]:
    """Fold 30-minute bars into hourly ones, grouping by wall-clock hour.

    Grouping by timestamp rather than by pairing adjacent bars keeps the
    boundaries correct across session gaps, where a naive pairwise fold would
    merge the last bar of one day with the first of the next.
    """
    buckets: Dict[Tuple, List[Bar]] = {}
    for b in bars:
        key = (b.ts.date(), b.ts.hour)
        buckets.setdefault(key, []).append(b)

    out: List[Bar] = []
    for key in sorted(buckets):
        group = sorted(buckets[key], key=lambda b: b.ts)
        out.append(Bar(
            ts=group[-1].ts,
            open=group[0].open,
            high=max(b.high for b in group),
            low=min(b.low for b in group),
            close=group[-1].close,
            volume=sum(b.volume for b in group),
        ))
    return out
