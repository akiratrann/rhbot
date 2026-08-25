"""Stock price feed via the UNOFFICIAL robin_stocks library.

Requires a Robinhood login (username/password, and MFA if enabled). Because it
is unofficial it can break when Robinhood changes their site, and heavy polling
may draw rate limits or account scrutiny — keep poll_interval_seconds sane.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import List, Optional

from ..models import Bar
from .feed import DataFeed

log = logging.getLogger("rhbot.stock")


class RobinhoodStockFeed(DataFeed):
    def __init__(self):
        # Lazy import so paper/synthetic runs don't need robin_stocks installed.
        import robin_stocks.robinhood as rh
        self._rh = rh

    def get_price(self, symbol: str) -> Optional[float]:
        try:
            vals = self._rh.stocks.get_latest_price(symbol)
            if vals and vals[0] is not None:
                return float(vals[0])
        except Exception as e:  # noqa: BLE001 — never crash the loop on data
            log.warning("stock price fetch failed for %s: %s", symbol, e)
        return None

    def get_bars(self, symbol: str, count: int) -> List[Bar]:
        try:
            # 5-minute bars over the last day/week give enough history for MAs.
            span = "week" if count > 78 else "day"
            hist = self._rh.stocks.get_stock_historicals(
                symbol, interval="5minute", span=span
            )
        except Exception as e:  # noqa: BLE001
            log.warning("stock historicals failed for %s: %s", symbol, e)
            return []

        bars: List[Bar] = []
        for h in hist or []:
            try:
                ts = datetime.fromisoformat(
                    h["begins_at"].replace("Z", "+00:00")
                ).astimezone(timezone.utc)
                bars.append(Bar(
                    ts=ts,
                    open=float(h["open_price"]),
                    high=float(h["high_price"]),
                    low=float(h["low_price"]),
                    close=float(h["close_price"]),
                    volume=float(h.get("volume", 0) or 0),
                ))
            except (KeyError, TypeError, ValueError):
                continue
        return bars[-count:]
