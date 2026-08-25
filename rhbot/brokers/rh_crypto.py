"""LIVE crypto broker — places real orders via the official Robinhood Crypto API.

Only constructed when mode=live and crypto credentials are present. Uses a
market order sized by dividing the requested notional by the current price.
"""

from __future__ import annotations

import logging
import uuid
from typing import Optional

from ..models import Fill, Order, Side
from ..rh_crypto_client import RobinhoodCryptoClient

log = logging.getLogger("rhbot.crypto")


class RobinhoodCryptoBroker:
    name = "robinhood-crypto"

    def __init__(self, client: RobinhoodCryptoClient):
        self.client = client

    def is_live(self) -> bool:
        return True

    def get_price(self, symbol: str) -> Optional[float]:
        return self.client.get_price(symbol)

    def submit(self, order: Order) -> Optional[Fill]:
        price = self.client.get_price(order.symbol)
        if not price or price <= 0:
            log.warning("crypto: no price for %s, order dropped", order.symbol)
            return None

        qty = round(order.notional / price, 8)
        coid = str(uuid.uuid4())
        log.info("LIVE crypto %s %s qty=%.8f (~$%.2f) coid=%s",
                 order.side.value.upper(), order.symbol, qty, order.notional, coid)

        resp = self.client.place_market_order(
            symbol=order.symbol, side=order.side.value,
            asset_quantity=qty, client_order_id=coid,
        )
        if not resp:
            log.error("crypto order REJECTED for %s", order.symbol)
            return None

        # Market orders fill ~immediately; we record at observed price. For exact
        # fills you would poll GET /orders/{id}; kept simple here on purpose.
        return Fill(symbol=order.symbol, asset_class=order.asset_class,
                    side=order.side, quantity=qty, price=price)
