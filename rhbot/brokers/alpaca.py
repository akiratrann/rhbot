"""Alpaca broker adapter — official API, stocks and crypto through one client.

Two things this does that the Robinhood adapters do not:

  * It reports the REAL fill (`filled_avg_price` / `filled_qty`) instead of
    assuming the order filled at the last observed price. Market orders slip,
    and a portfolio built on assumed prices drifts away from the truth a little
    on every trade.
  * Sells are clamped to the quantity Alpaca actually shows as available, so a
    price move between the signal and the order can't turn a full exit into a
    rejected "insufficient qty".

`paper=True` on the client points at Alpaca's paper host: a real matching
engine with fake money, no PDT cap and no settlement. That is the recommended
place to run this until the strategy has earned real money.
"""

from __future__ import annotations

import logging
import uuid
from typing import Dict, Optional

from ..alpaca_client import AlpacaClient, from_alpaca_symbol
from ..models import AssetClass, Fill, Order, Side
from .base import Broker

log = logging.getLogger("rhbot.alpaca")


#: Quote currencies that make a dashed symbol a crypto pair. Needed because
#: dashes also appear in share classes — `BRK-B` is a stock, `BTC-USD` is not.
CRYPTO_QUOTES = ("USD", "USDT", "USDC", "BTC", "ETH")


class AlpacaBroker(Broker):
    def __init__(self, client: AlpacaClient,
                 symbol_class: Optional[Dict[str, AssetClass]] = None):
        self.client = client
        self.symbol_class = symbol_class or {}
        self.name = "alpaca-paper" if client.paper else "alpaca-live"

    def is_live(self) -> bool:
        """True only for real money. Alpaca's paper host trades nothing real."""
        return not self.client.paper

    def get_price(self, symbol: str) -> Optional[float]:
        return self.client.get_price(symbol, self._is_crypto(symbol))

    # ---- helpers ----------------------------------------------------------

    @staticmethod
    def _is_crypto_class(asset_class: AssetClass) -> bool:
        return asset_class == AssetClass.CRYPTO

    def _is_crypto(self, symbol: str) -> bool:
        """Price lookups arrive without an asset class, so classify by symbol.

        The configured watchlist is authoritative; the suffix check is only a
        fallback for symbols the caller never declared.
        """
        known = self.symbol_class.get(symbol)
        if known is not None:
            return known == AssetClass.CRYPTO
        parts = symbol.replace("/", "-").split("-")
        return len(parts) == 2 and parts[1].upper() in CRYPTO_QUOTES

    def _available_qty(self, symbol: str, is_crypto: bool) -> Optional[float]:
        pos = self.client.get_position(symbol, is_crypto)
        if not pos:
            return None
        try:
            # `qty_available` excludes shares already committed to open orders.
            return float(pos.get("qty_available", pos.get("qty", 0)))
        except (TypeError, ValueError):
            return None

    # ---- order submission -------------------------------------------------

    def submit(self, order: Order) -> Optional[Fill]:
        is_crypto = self._is_crypto_class(order.asset_class)

        # A `day` market order placed outside the session sits in `new` until
        # the open. await_fill would time out and cancel it, the engine would
        # re-signal on the next tick, and the pair would churn orders at the
        # broker indefinitely. Refuse up front instead. Crypto trades 24/7 and
        # is exempt. An unreachable clock returns None — treat that as "don't
        # know" and let the order through rather than halting on a blip.
        if not is_crypto and self.client.is_market_open() is False:
            log.info("alpaca: equities session closed — %s %s deferred",
                     order.side.value.upper(), order.symbol)
            return None

        coid = f"rhbot-{uuid.uuid4()}"

        if order.side == Side.BUY:
            kwargs = {"notional": order.notional}
            size_desc = f"${order.notional:.2f}"
        else:
            price = self.client.get_price(order.symbol, is_crypto)
            if not price or price <= 0:
                log.warning("alpaca: no price for %s, sell dropped", order.symbol)
                return None
            qty = order.notional / price

            # Never try to sell more than the broker says we hold.
            available = self._available_qty(order.symbol, is_crypto)
            if available is not None:
                if available <= 0:
                    log.warning("alpaca: no position in %s to sell", order.symbol)
                    return None
                qty = min(qty, available)

            qty = round(qty, 9)
            if qty <= 0:
                return None
            kwargs = {"qty": qty}
            size_desc = f"{qty:.9f}"

        log.info("%s %s %s %s coid=%s", self.name.upper(),
                 order.side.value.upper(), order.symbol, size_desc, coid)

        placed = self.client.submit_market_order(
            symbol=order.symbol, side=order.side.value, is_crypto=is_crypto,
            client_order_id=coid, **kwargs,
        )
        if not placed or not placed.get("id"):
            log.error("alpaca order REJECTED for %s", order.symbol)
            return None

        final = self.client.await_fill(placed["id"])
        return self._to_fill(order, final)

    def _to_fill(self, order: Order, final: Optional[dict]) -> Optional[Fill]:
        if not final:
            log.error("alpaca: lost track of order for %s — RECONCILE MANUALLY "
                      "before trusting the local portfolio", order.symbol)
            return None

        try:
            filled_qty = float(final.get("filled_qty") or 0)
            avg_price = float(final.get("filled_avg_price") or 0)
        except (TypeError, ValueError):
            filled_qty, avg_price = 0.0, 0.0

        if filled_qty <= 0 or avg_price <= 0:
            log.warning("alpaca %s %s did not fill (status=%s)",
                        order.side.value, order.symbol, final.get("status"))
            return None

        if final.get("status") != "filled":
            log.warning("alpaca %s %s PARTIAL: %.9f filled (status=%s) — "
                        "booking the partial", order.side.value, order.symbol,
                        filled_qty, final.get("status"))

        log.info("alpaca FILL %s %s %.9f @ %.4f ($%.2f)",
                 order.side.value.upper(), order.symbol, filled_qty, avg_price,
                 filled_qty * avg_price)
        return Fill(symbol=order.symbol, asset_class=order.asset_class,
                    side=order.side, quantity=filled_qty, price=avg_price)

    # ---- account introspection --------------------------------------------

    def held_quantities(self) -> Dict[str, float]:
        """Broker-side positions keyed by rhbot symbol.

        The positions endpoint returns crypto concatenated (`BTCUSD`), which
        matches neither the order form (`BTC/USD`) nor ours (`BTC-USD`). It
        does report `asset_class`, so use that rather than guessing from shape.
        """
        out: Dict[str, float] = {}
        for pos in self.client.list_positions():
            sym = str(pos.get("symbol", ""))
            if not sym:
                continue
            try:
                qty = float(pos.get("qty") or 0)
            except (TypeError, ValueError):
                continue
            is_crypto = str(pos.get("asset_class", "")).lower() == "crypto"
            out[from_alpaca_symbol(sym, is_crypto)] = qty
        return out

    def position_details(self) -> Dict[str, dict]:
        """Positions with cost basis, keyed by rhbot symbol.

        Richer than `held_quantities` because seeding a fresh book needs the
        average entry price too — without it, realised P&L on the first sale
        would be computed against a basis of zero.
        """
        out: Dict[str, dict] = {}
        for pos in self.client.list_positions():
            sym = str(pos.get("symbol", ""))
            if not sym:
                continue
            is_crypto = str(pos.get("asset_class", "")).lower() == "crypto"
            try:
                out[from_alpaca_symbol(sym, is_crypto)] = {
                    "quantity": float(pos.get("qty") or 0),
                    "avg_price": float(pos.get("avg_entry_price") or 0),
                    "asset_class": (AssetClass.CRYPTO if is_crypto
                                    else AssetClass.STOCK),
                }
            except (TypeError, ValueError):
                continue
        return out

    def account_snapshot(self) -> Optional[dict]:
        """Equity, day-trade count and PDT flag straight from the broker.

        The engine tracks day trades locally, but Alpaca is the authority — it
        sees trades this bot did not place.
        """
        acct = self.client.get_account()
        if not acct:
            return None
        try:
            equity = float(acct.get("equity") or 0)
        except (TypeError, ValueError):
            equity = 0.0
        return {
            "equity": equity,
            "cash": acct.get("cash"),
            "buying_power": acct.get("buying_power"),
            "daytrade_count": int(acct.get("daytrade_count") or 0),
            "pattern_day_trader": bool(acct.get("pattern_day_trader")),
            "blocked": bool(acct.get("trading_blocked")
                            or acct.get("account_blocked")),
            "status": acct.get("status"),
        }
