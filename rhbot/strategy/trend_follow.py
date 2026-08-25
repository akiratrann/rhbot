"""Trend following — stay long while the trend holds, step aside when it breaks.

Why this exists: every other strategy here is mean-reverting. They sell into
strength, which is exactly the wrong reflex in a bull market — backtests on
AAPL/NVDA/MSFT had them returning single digits while buy & hold returned
71%/402%/57%. A rule that *rides* trends instead of fading them is the missing
piece, and it is the only one in this repo that can plausibly keep up with
holding the stock.

Rules:
  ENTER_LONG when price closes above its `fast` SMA **and** the `fast` SMA is
    above the `slow` SMA (trend is up and confirmed on two timescales).
  EXIT_LONG when price closes below the `exit_ma` SMA, optionally widened by a
    `stop_atr_mult` multiple of recent range so ordinary noise doesn't shake
    you out of an intact trend.

The asymmetry is deliberate: enter on confirmation (slow, few false starts),
exit on a looser band (stay in as long as the trend is arguably alive). Trend
systems make their money from a small number of large winners, so the cost of
exiting early is much higher than the cost of holding a little too long.

Expect a LOW win rate. That is normal and not a bug — many small losses funding
a few large gains. Judge it on total return and drawdown, never on win rate.

TEMPLATE, not advice. Tune and backtest before trusting it.
"""

from __future__ import annotations

from typing import List, Optional

from ..indicators import sma
from ..models import Bar, Position, Signal, SignalType
from .base import Strategy


def average_range(bars: List[Bar], period: int) -> Optional[float]:
    """Mean high-low range over the last `period` bars — a volatility yardstick.

    Deliberately not a true ATR: no gap handling, because the exit band only
    needs a rough sense of "how much does this thing move on a normal day".
    """
    if len(bars) < period or period <= 0:
        return None
    window = bars[-period:]
    return sum(b.high - b.low for b in window) / period


class TrendFollow(Strategy):
    def __init__(self, params: Optional[dict] = None):
        super().__init__(params)
        self.fast = int(self.params.get("fast", 20))
        self.slow = int(self.params.get("slow", 100))
        self.exit_ma = int(self.params.get("exit_ma", 50))
        self.range_period = int(self.params.get("range_period", 14))
        #: Widen the exit band by this many average ranges. 0 disables it.
        self.stop_atr_mult = float(self.params.get("stop_atr_mult", 0.0))

        if self.fast >= self.slow:
            raise ValueError(
                f"fast ({self.fast}) must be shorter than slow ({self.slow})"
            )
        # Only demand range history when the volatility band is actually in use;
        # otherwise a disabled stop needlessly delays the first signal.
        needed = [self.slow, self.exit_ma]
        if self.stop_atr_mult > 0:
            needed.append(self.range_period)
        self.warmup_bars = max(needed) + 2

    def evaluate(self, symbol: str, bars: List[Bar],
                 position: Optional[Position]) -> Signal:
        if len(bars) < self.warmup_bars:
            return Signal(symbol, SignalType.HOLD, "warming up")

        closes = [b.close for b in bars]
        fast_line = sma(closes, self.fast)
        slow_line = sma(closes, self.slow)
        exit_line = sma(closes, self.exit_ma)

        price = closes[-1]
        fast_now, slow_now, exit_now = fast_line[-1], slow_line[-1], exit_line[-1]
        if fast_now is None or slow_now is None or exit_now is None:
            return Signal(symbol, SignalType.HOLD, "indicators not ready")

        holding = position is not None and position.quantity > 0

        if holding:
            band = exit_now
            if self.stop_atr_mult > 0:
                rng = average_range(bars, self.range_period)
                if rng is not None:
                    band = exit_now - self.stop_atr_mult * rng
            if price < band:
                return Signal(symbol, SignalType.EXIT_LONG,
                              f"close {price:.2f} below exit band {band:.2f} "
                              f"(SMA{self.exit_ma})")
            return Signal(symbol, SignalType.HOLD, "trend intact, riding it")

        if price > fast_now and fast_now > slow_now:
            return Signal(symbol, SignalType.ENTER_LONG,
                          f"price {price:.2f} > SMA{self.fast} {fast_now:.2f} "
                          f"> SMA{self.slow} {slow_now:.2f}")
        return Signal(symbol, SignalType.HOLD, "no confirmed uptrend")
