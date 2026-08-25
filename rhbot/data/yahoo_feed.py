"""Free market data from Yahoo's public chart endpoint — no API key, no login.

This is the default data source for PAPER mode, so you get REAL prices and
realistic strategy behaviour before ever putting credentials on disk. It covers
both stocks (AAPL) and crypto (BTC-USD).

Not for live execution — it is an unofficial endpoint with no SLA, and quotes
may lag. Live mode uses the Robinhood feeds instead.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

import requests

from ..models import Bar
from .feed import DataFeed

log = logging.getLogger("rhbot.yahoo")

_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"


class YahooFeed(DataFeed):
    def __init__(self, interval: str = "1m", range_: str = "1d",
                 cache_seconds: int = 30):
        self.interval = interval
        self.range = range_
        self.cache_seconds = cache_seconds
        self._cache: Dict[str, Tuple[float, List[Bar]]] = {}

    def _fetch(self, symbol: str) -> List[Bar]:
        now = time.time()
        hit = self._cache.get(symbol)
        if hit and (now - hit[0]) < self.cache_seconds:
            return hit[1]

        bars = self._fetch_raw(symbol, self.interval, self.range)

        # Yahoo publishes the CURRENT session's daily bar with a null close
        # until it settles, so a plain daily fetch silently lags by a session.
        # Rebuild that bar from intraday data so strategies see today.
        if self.interval == "1d" and bars:
            bars = self._complete_current_session(symbol, bars)

        if bars:
            self._cache[symbol] = (now, bars)
            return bars
        return hit[1] if hit else []

    def _complete_current_session(self, symbol: str,
                                  daily: List[Bar]) -> List[Bar]:
        """Append today's in-progress daily bar, aggregated from 1-minute bars."""
        intraday = self._fetch_raw(symbol, "1m", "5d")
        if not intraday:
            return daily

        last_daily = daily[-1].ts.date()
        by_day: Dict[str, List[Bar]] = {}
        for b in intraday:
            if b.ts.date() > last_daily:
                by_day.setdefault(b.ts.date().isoformat(), []).append(b)

        for day in sorted(by_day):
            session = by_day[day]
            daily.append(Bar(
                ts=session[-1].ts,
                open=session[0].open,
                high=max(b.high for b in session),
                low=min(b.low for b in session),
                close=session[-1].close,
                volume=sum(b.volume for b in session),
            ))
            log.debug("%s: completed daily bar for %s from %d intraday bars",
                      symbol, day, len(session))
        return daily

    def _fetch_raw(self, symbol: str, interval: str,
                   range_: str) -> List[Bar]:
        try:
            resp = requests.get(
                _URL.format(symbol=symbol),
                params={"range": range_, "interval": interval},
                headers={"User-Agent": "Mozilla/5.0"}, timeout=20,
            )
            if resp.status_code != 200:
                log.warning("yahoo %s -> HTTP %s", symbol, resp.status_code)
                return []
            result = resp.json()["chart"]["result"][0]
            stamps = result["timestamp"]
            q = result["indicators"]["quote"][0]
        except (requests.RequestException, KeyError, TypeError, ValueError) as e:
            log.warning("yahoo fetch failed for %s: %s", symbol, e)
            return []

        bars: List[Bar] = []
        for i, ts in enumerate(stamps):
            c = q["close"][i]
            if c is None:
                continue  # Yahoo emits nulls for gaps/halts
            bars.append(Bar(
                ts=datetime.fromtimestamp(ts, tz=timezone.utc),
                open=float(q["open"][i] if q["open"][i] is not None else c),
                high=float(q["high"][i] if q["high"][i] is not None else c),
                low=float(q["low"][i] if q["low"][i] is not None else c),
                close=float(c),
                volume=float(q["volume"][i] or 0) if q.get("volume") else 0.0,
            ))
        return bars

    def get_price(self, symbol: str) -> Optional[float]:
        bars = self._fetch(symbol)
        return bars[-1].close if bars else None

    def get_bars(self, symbol: str, count: int) -> List[Bar]:
        return self._fetch(symbol)[-count:]
