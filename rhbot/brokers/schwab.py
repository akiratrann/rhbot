"""LIVE broker adapter for the official Charles Schwab Trader API.

There is no paper mode on this API — Schwab's sandbox serves synthetic data and
is not a simulated-trading account. Every order this adapter sends is real. Do
the strategy work on `broker: alpaca` + `mode: paper` and switch here only when
you mean it.

The important behavioural difference from the other adapters: **Schwab trades
whole shares only**. The rest of this codebase sizes orders in dollars, so the
notional is floored to a share count here. A $100 order on a $300 stock buys
nothing, and says so rather than failing silently.
"""

from __future__ import annotations

import logging
import math
from typing import Dict, Optional

from ..models import AssetClass, Fill, Order, Side
from ..schwab_client import SchwabClient, average_fill_price
from .base import Broker

log = logging.getLogger("rhbot.schwab")

#: Guards against float noise turning an exact 3.0 shares into 2.
_SHARE_EPSILON = 1e-6


class SchwabBroker(Broker):
    name = "schwab"

    def __init__(self, client: SchwabClient):
        self.client = client

    def is_live(self) -> bool:
        """Always. Schwab's API has no paper account behind it."""
        return True

    def get_price(self, symbol: str) -> Optional[float]:
        return self.client.get_quote(symbol)

    # ---- positions --------------------------------------------------------

    def held_quantities(self) -> Dict[str, float]:
        acct = self.client.get_account(with_positions=True) or {}
        out: Dict[str, float] = {}
        for pos in acct.get("positions") or []:
            symbol = ((pos.get("instrument") or {}).get("symbol") or "").strip()
            if not symbol:
                continue
            try:
                qty = float(pos.get("longQuantity") or 0)
            except (TypeError, ValueError):
                continue
            if qty:
                out[symbol] = qty
        return out

    def _held(self, symbol: str) -> float:
        return self.held_quantities().get(symbol, 0.0)

    # ---- order submission -------------------------------------------------

    def submit(self, order: Order) -> Optional[Fill]:
        if order.asset_class == AssetClass.CRYPTO:
            log.error("schwab: no crypto on this API — %s dropped", order.symbol)
            return None

        price = self.client.get_quote(order.symbol)
        if not price or price <= 0:
            log.warning("schwab: no quote for %s, order dropped", order.symbol)
            return None

        qty = int(math.floor(order.notional / price + _SHARE_EPSILON))

        if order.side == Side.SELL:
            held = self._held(order.symbol)
            if held <= 0:
                log.warning("schwab: no position in %s to sell", order.symbol)
                return None
            # Never try to sell more than the account actually holds; a quote
            # moving between signal and order would otherwise overshoot.
            qty = min(qty, int(math.floor(held + _SHARE_EPSILON)))

        if qty < 1:
            log.warning(
                "schwab: $%.2f of %s at $%.2f rounds to 0 whole shares — "
                "order skipped. Schwab has no fractional orders on this API; "
                "raise order_notional above the share price.",
                order.notional, order.symbol, price)
            return None

        instruction = "BUY" if order.side == Side.BUY else "SELL"
        log.info("SCHWAB LIVE %s %s x%d (~$%.2f)",
                 instruction, order.symbol, qty, qty * price)

        order_id = self.client.place_equity_market_order(
            symbol=order.symbol, instruction=instruction, quantity=qty)
        if not order_id:
            log.error("schwab order REJECTED for %s", order.symbol)
            return None

        final = self.client.await_fill(order_id)
        return self._to_fill(order, final)

    def _to_fill(self, order: Order, final: Optional[dict]) -> Optional[Fill]:
        if not final:
            log.error("schwab: lost track of the order for %s — RECONCILE "
                      "MANUALLY before trusting the local portfolio",
                      order.symbol)
            return None

        filled_qty, avg_price = average_fill_price(final)
        if filled_qty <= 0 or avg_price <= 0:
            log.warning("schwab %s %s did not fill (status=%s)",
                        order.side.value, order.symbol, final.get("status"))
            return None

        if str(final.get("status", "")).upper() != "FILLED":
            log.warning("schwab %s %s PARTIAL: %.4f filled (status=%s) — "
                        "booking the partial", order.side.value, order.symbol,
                        filled_qty, final.get("status"))

        log.info("schwab FILL %s %s %.4f @ %.4f ($%.2f)",
                 order.side.value.upper(), order.symbol, filled_qty, avg_price,
                 filled_qty * avg_price)
        return Fill(symbol=order.symbol, asset_class=order.asset_class,
                    side=order.side, quantity=filled_qty, price=avg_price)

    # ---- account introspection --------------------------------------------

    def account_snapshot(self) -> Optional[dict]:
        """Equity and day-trade state as Schwab sees it.

        `roundTrips` is Schwab's own day-trade counter. It sees trades this bot
        did not place, so where it disagrees with the local tally, it wins.
        """
        acct = self.client.get_account()
        if not acct:
            return None
        balances = acct.get("currentBalances") or {}
        try:
            equity = float(balances.get("liquidationValue") or 0)
        except (TypeError, ValueError):
            equity = 0.0
        return {
            "equity": equity,
            "buying_power": balances.get("buyingPower"),
            "daytrade_count": int(acct.get("roundTrips") or 0),
            "pattern_day_trader": bool(acct.get("isDayTrader")),
            "blocked": bool(acct.get("isClosingOnlyRestricted")),
            "status": acct.get("type"),
        }
