"""Paper broker: simulated fills against REAL market prices.

Prices come from a live data feed, so signals behave exactly as they would in
production — only the execution is simulated. This is the default and the only
broker that runs without credentials.
"""

from __future__ import annotations

import logging
from typing import Optional

from ..data.feed import DataFeed
from ..models import Fill, Order, Side

log = logging.getLogger("rhbot.paper")


class PaperBroker:
    name = "paper"

    def __init__(self, feed: DataFeed, slippage_bps: float = 2.0):
        """`slippage_bps`: modelled slippage in basis points (2 = 0.02%)."""
        self.feed = feed
        self.slippage = slippage_bps / 10_000.0

    def is_live(self) -> bool:
        return False

    def get_price(self, symbol: str) -> Optional[float]:
        return self.feed.get_price(symbol)

    def submit(self, order: Order) -> Optional[Fill]:
        price = self.feed.get_price(order.symbol)
        if price is None or price <= 0:
            log.warning("paper: no price for %s, order dropped", order.symbol)
            return None

        # Model slippage: buys fill slightly higher, sells slightly lower.
        if order.side == Side.BUY:
            fill_price = price * (1 + self.slippage)
        else:
            fill_price = price * (1 - self.slippage)

        qty = order.notional / fill_price
        fill = Fill(
            symbol=order.symbol,
            asset_class=order.asset_class,
            side=order.side,
            quantity=qty,
            price=fill_price,
        )
        log.info("paper FILL %s %s %.6f @ %.4f ($%.2f)",
                 order.side.value.upper(), order.symbol, qty, fill_price,
                 fill.notional)
        return fill
