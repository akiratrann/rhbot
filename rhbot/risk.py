"""Risk manager — the last gate before any order is sent.

Every order passes through `check()`. If it returns a reason string, the order
is BLOCKED and never reaches the broker. This is deliberately conservative: when
in doubt, it blocks. The kill switch and daily-loss cap can halt ALL trading.
"""

from __future__ import annotations

import logging
import os
from typing import Dict, Optional

from .config import RiskConfig
from .models import AssetClass, Order, Side
from .portfolio import Portfolio, market_date

log = logging.getLogger("rhbot.risk")


class RiskManager:
    #: A daily-loss halt stops NEW RISK but must never trap you in a position.
    #: A kill switch is a human saying "freeze everything" and does stop exits.
    HALT_ENTRIES_ONLY = "entries"
    HALT_EVERYTHING = "all"

    def __init__(self, cfg: RiskConfig):
        self.cfg = cfg
        self._halted = False
        self._halt_reason = ""
        self._halt_kind = self.HALT_EVERYTHING

    # ---- global halts -----------------------------------------------------

    def halted(self) -> bool:
        return self._halted

    def halt_reason(self) -> str:
        return self._halt_reason

    def _halt(self, reason: str, kind: str = "all") -> None:
        if not self._halted:
            log.error("TRADING HALTED (%s): %s",
                      "exits still allowed" if kind == "entries"
                      else "including exits", reason)
        self._halted = True
        self._halt_reason = reason
        self._halt_kind = kind

    def blocks_exits(self) -> bool:
        return self._halted and self._halt_kind == self.HALT_EVERYTHING

    def check_global_halts(self, portfolio: Portfolio,
                           prices: Dict[str, float]) -> bool:
        """Evaluate kill switch + daily loss. Returns True if halted."""
        if os.path.exists(self.cfg.kill_switch_file):
            # Explicit human freeze: stop everything, exits included.
            self._halt(f"kill switch file present ({self.cfg.kill_switch_file})",
                       self.HALT_EVERYTHING)
            return True

        daily = portfolio.daily_pnl(prices)
        if daily <= self.cfg.max_daily_loss:
            # Stop taking NEW risk, but keep evaluating exits. This fires
            # precisely when things are going badly, which is the moment you
            # most need to be able to get out.
            self._halt(f"daily loss {daily:.2f} hit limit "
                       f"{self.cfg.max_daily_loss:.2f}",
                       self.HALT_ENTRIES_ONLY)
            return False
        return False

    # ---- per-order checks -------------------------------------------------

    def check(self, order: Order, portfolio: Portfolio,
              prices: Dict[str, float]) -> Optional[str]:
        """Return a rejection reason, or None if the order is allowed."""
        if self.blocks_exits():
            return f"trading halted: {self._halt_reason}"
        if self._halted and order.side == Side.BUY:
            return f"no new entries: {self._halt_reason}"

        if order.notional <= 0:
            return "non-positive notional"

        # Exits (sells) are always allowed through the size/exposure gates —
        # reducing risk should never be blocked. Only entries are constrained.
        #
        # max_order_notional is checked BELOW, on the buy side only, and that
        # ordering is load-bearing: an exit is sized at the position's CURRENT
        # market value, so a winner that appreciates past the cap would fail
        # the check and become impossible to sell. Capping entries bounds the
        # risk; capping exits just traps you in the biggest positions.
        if order.side == Side.SELL:
            pos = portfolio.positions.get(order.symbol)
            if not pos or pos.quantity <= 0:
                return "no position to sell"
            # Deliberate trade-off: if this exit would breach the day-trade
            # budget we WARN but still allow it. Blocking an exit could trap you
            # in a losing position, which is a worse outcome than a PDT flag.
            # The budget is instead protected by refusing new ENTRIES below.
            if (self.cfg.pdt_guard
                    and order.asset_class != AssetClass.CRYPTO
                    and pos.last_buy_date == market_date()
                    and portfolio.day_trades_in_window()
                    >= self.cfg.max_day_trades_per_5_days):
                log.warning(
                    "PDT: selling %s today is day trade #%d in 5 business days "
                    "(limit %d). ALLOWING the exit — but your broker may flag "
                    "the account.", order.symbol,
                    portfolio.day_trades_in_window() + 1,
                    self.cfg.max_day_trades_per_5_days)
            return None

        # BUY-side checks.
        if order.notional > self.cfg.max_order_notional:
            return (f"notional {order.notional:.2f} > max_order_notional "
                    f"{self.cfg.max_order_notional:.2f}")

        # PDT guard: once the day-trade budget is spent, stop opening NEW stock
        # positions — an intraday strategy would likely need to close them the
        # same day, which is exactly what breaches the limit. Crypto is exempt.
        if (self.cfg.pdt_guard
                and order.asset_class != AssetClass.CRYPTO):
            used = portfolio.day_trades_in_window()
            if used >= self.cfg.max_day_trades_per_5_days:
                return (f"PDT guard: {used} day trades used in the last 5 "
                        f"business days (limit "
                        f"{self.cfg.max_day_trades_per_5_days}); "
                        f"no new stock entries")

        open_positions = portfolio.open_positions()
        is_new_symbol = order.symbol not in {p.symbol for p in open_positions}
        if is_new_symbol and len(open_positions) >= self.cfg.max_open_positions:
            return (f"max_open_positions {self.cfg.max_open_positions} reached")

        projected_exposure = portfolio.exposure(prices) + order.notional
        if projected_exposure > self.cfg.max_total_exposure:
            return (f"projected exposure {projected_exposure:.2f} > "
                    f"max_total_exposure {self.cfg.max_total_exposure:.2f}")

        if order.notional > portfolio.cash:
            return (f"insufficient cash: need {order.notional:.2f}, "
                    f"have {portfolio.cash:.2f}")

        return None
