"""Slope-reversal strategy — buy local minima, sell local maxima.

The rule you asked for, stated precisely:
  - The "line" is the price, SMOOTHED over `smooth` bars to kill tick noise.
  - We take its discrete derivative (slope) = smoothed[t] - smoothed[t-1].
  - Slope flips POSITIVE -> NEGATIVE  => a local top  => SELL (exit long).
  - Slope flips NEGATIVE -> POSITIVE  => a local bottom => BUY (enter long).

Two deliberate design choices, because the naive version self-destructs:

  1. SMOOTHING. Taking the derivative of raw price makes the sign flip on nearly
     every bar (noise), producing constant whipsaw. `smooth` denoises first.
     Bigger smooth = fewer, higher-quality flips but more lag. Tune it.

  2. FREQUENCY is enforced OUTSIDE this strategy, by the engine's per-symbol
     cooldown + the risk manager. This file only decides direction; it never
     decides "how often" — that keeps you inside Robinhood's limits (see README:
     PDT rule for stocks under $25k). Crypto has no day-trade cap.

This remains a TEMPLATE. Reacting to every local extreme is a high-churn idea
that loses to fees/slippage in choppy markets. BACKTEST before going live.
"""

from __future__ import annotations

from typing import List, Optional

from ..indicators import sma
from ..models import Bar, Position, Signal, SignalType
from .base import Strategy


class SlopeReversal(Strategy):
    def __init__(self, params: Optional[dict] = None):
        super().__init__(params)
        self.smooth = max(1, int(self.params.get("smooth", 3)))
        # Require the slope to exceed this fraction of price to count as a real
        # move (deadband). 0 = react to any flip. e.g. 0.0005 = 0.05%.
        self.min_slope_pct = float(self.params.get("min_slope_pct", 0.0))
        self.warmup_bars = self.smooth + 3

    def _slope(self, smoothed: List[Optional[float]], i: int) -> Optional[float]:
        a, b = smoothed[i - 1], smoothed[i]
        if a is None or b is None:
            return None
        return b - a

    def evaluate(self, symbol: str, bars: List[Bar],
                 position: Optional[Position]) -> Signal:
        if len(bars) < self.warmup_bars:
            return Signal(symbol, SignalType.HOLD, "warming up")

        closes = [b.close for b in bars]
        line = sma(closes, self.smooth) if self.smooth > 1 else closes
        # Ensure list-of-optional shape when smooth == 1.
        if self.smooth == 1:
            line = [float(c) for c in closes]

        n = len(line)
        slope_now = self._slope(line, n - 1)
        slope_prev = self._slope(line, n - 2)
        if slope_now is None or slope_prev is None:
            return Signal(symbol, SignalType.HOLD, "no slope yet")

        last_price = closes[-1] or 1.0
        deadband = self.min_slope_pct * last_price
        holding = position is not None and position.quantity > 0

        # Positive -> negative: local top -> SELL.
        if slope_prev > 0 and slope_now < -deadband:
            if holding:
                return Signal(symbol, SignalType.EXIT_LONG,
                              "slope turned +->- (local top)")
            return Signal(symbol, SignalType.HOLD, "top but flat (no short)")

        # Negative -> positive: local bottom -> BUY.
        if slope_prev < 0 and slope_now > deadband:
            if not holding:
                return Signal(symbol, SignalType.ENTER_LONG,
                              "slope turned -->+ (local bottom)")
            return Signal(symbol, SignalType.HOLD, "bottom but already long")

        return Signal(symbol, SignalType.HOLD, "no slope flip")
