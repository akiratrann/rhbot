"""Example strategy: simple moving-average crossover.

  - Fast SMA crosses ABOVE slow SMA  -> ENTER_LONG (if flat)
  - Fast SMA crosses BELOW slow SMA  -> EXIT_LONG  (if holding)

This is a TEMPLATE, not advice. It is a well-known toy strategy that will lose
money in choppy markets. Edit the logic, add filters (RSI, ATR stops, trend
regime), and BACKTEST before ever running it live. Copy this file to make your
own strategy, then register it in strategy/__init__.py.
"""

from __future__ import annotations

from typing import List, Optional

from ..indicators import crossed_above, crossed_below, sma
from ..models import Bar, Position, Signal, SignalType
from .base import Strategy


class SmaCrossover(Strategy):
    def __init__(self, params: Optional[dict] = None):
        super().__init__(params)
        self.fast = int(self.params.get("fast", 10))
        self.slow = int(self.params.get("slow", 30))
        if self.fast >= self.slow:
            raise ValueError("fast period must be < slow period")
        self.warmup_bars = self.slow + 1

    def evaluate(self, symbol: str, bars: List[Bar],
                 position: Optional[Position]) -> Signal:
        if len(bars) < self.warmup_bars:
            return Signal(symbol, SignalType.HOLD, "warming up")

        closes = [b.close for b in bars]
        fast_line = sma(closes, self.fast)
        slow_line = sma(closes, self.slow)
        holding = position is not None and position.quantity > 0

        if crossed_above(fast_line, slow_line) and not holding:
            return Signal(symbol, SignalType.ENTER_LONG,
                          f"SMA{self.fast} crossed above SMA{self.slow}")
        if crossed_below(fast_line, slow_line) and holding:
            return Signal(symbol, SignalType.EXIT_LONG,
                          f"SMA{self.fast} crossed below SMA{self.slow}")
        return Signal(symbol, SignalType.HOLD, "no cross")
