"""Swing strategy — slower signals intended to be HELD OVERNIGHT.

Why this exists: positions held past the close are NOT day trades, so they don't
consume your 3-per-5-days PDT budget (see README). This trades on trend
persistence rather than every local wiggle.

Rules:
  ENTER_LONG when BOTH:
    - trend filter: price is above its long SMA (`trend`), i.e. uptrend, AND
    - momentum turns up: RSI crosses above `rsi_entry` from below (oversold bounce)
  EXIT_LONG when EITHER:
    - RSI crosses above `rsi_exit` (overbought — take profit), OR
    - price closes below the long SMA (trend broke — cut it)

TEMPLATE, not advice. Tune and backtest. Parameters are all overridable.
"""

from __future__ import annotations

from typing import List, Optional

from ..indicators import rsi, sma
from ..models import Bar, Position, Signal, SignalType
from .base import Strategy


class SwingTrend(Strategy):
    def __init__(self, params: Optional[dict] = None):
        super().__init__(params)
        self.trend = int(self.params.get("trend", 50))
        self.rsi_period = int(self.params.get("rsi_period", 14))
        self.rsi_entry = float(self.params.get("rsi_entry", 35.0))
        self.rsi_exit = float(self.params.get("rsi_exit", 70.0))
        self.warmup_bars = max(self.trend, self.rsi_period) + 2

    def evaluate(self, symbol: str, bars: List[Bar],
                 position: Optional[Position]) -> Signal:
        if len(bars) < self.warmup_bars:
            return Signal(symbol, SignalType.HOLD, "warming up")

        closes = [b.close for b in bars]
        trend_line = sma(closes, self.trend)
        rsi_line = rsi(closes, self.rsi_period)

        price = closes[-1]
        trend_now = trend_line[-1]
        r_now, r_prev = rsi_line[-1], rsi_line[-2]
        if trend_now is None or r_now is None or r_prev is None:
            return Signal(symbol, SignalType.HOLD, "indicators not ready")

        holding = position is not None and position.quantity > 0

        if holding:
            if r_prev <= self.rsi_exit < r_now:
                return Signal(symbol, SignalType.EXIT_LONG,
                              f"RSI {r_now:.0f} crossed above {self.rsi_exit:.0f}")
            if price < trend_now:
                return Signal(symbol, SignalType.EXIT_LONG,
                              f"price below SMA{self.trend} (trend broke)")
            return Signal(symbol, SignalType.HOLD, "holding trend")

        uptrend = price > trend_now
        bounce = r_prev <= self.rsi_entry < r_now
        if uptrend and bounce:
            return Signal(symbol, SignalType.ENTER_LONG,
                          f"uptrend + RSI bounce above {self.rsi_entry:.0f}")
        return Signal(symbol, SignalType.HOLD, "no setup")
