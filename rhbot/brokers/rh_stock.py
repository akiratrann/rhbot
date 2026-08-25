"""LIVE stock broker via the UNOFFICIAL robin_stocks library.

Only constructed when mode=live and stock credentials are present. Places
fractional market orders sized by dollar amount (order_dollar_limit_buy /
order_sell_fractional_by_price).
"""

from __future__ import annotations

import logging
from typing import Optional

from ..data.rh_stock_feed import RobinhoodStockFeed
from ..models import Fill, Order, Side

log = logging.getLogger("rhbot.stock")


class RobinhoodStockBroker:
    name = "robinhood-stock"

    def __init__(self, feed: RobinhoodStockFeed):
        import robin_stocks.robinhood as rh
        self._rh = rh
        self.feed = feed

    def is_live(self) -> bool:
        return True

    def get_price(self, symbol: str) -> Optional[float]:
        return self.feed.get_price(symbol)

    def submit(self, order: Order) -> Optional[Fill]:
        price = self.feed.get_price(order.symbol)
        if not price or price <= 0:
            log.warning("stock: no price for %s, order dropped", order.symbol)
            return None

        log.info("LIVE stock %s %s ~$%.2f @ ~%.2f",
                 order.side.value.upper(), order.symbol, order.notional, price)
        try:
            if order.side == Side.BUY:
                resp = self._rh.orders.order_buy_fractional_by_price(
                    order.symbol, order.notional, timeInForce="gfd"
                )
            else:
                resp = self._rh.orders.order_sell_fractional_by_price(
                    order.symbol, order.notional, timeInForce="gfd"
                )
        except Exception as e:  # noqa: BLE001
            log.error("stock order error for %s: %s", order.symbol, e)
            return None

        if not resp or resp.get("detail"):
            log.error("stock order REJECTED for %s: %s", order.symbol,
                      (resp or {}).get("detail"))
            return None

        qty = order.notional / price
        return Fill(symbol=order.symbol, asset_class=order.asset_class,
                    side=order.side, quantity=qty, price=price)
