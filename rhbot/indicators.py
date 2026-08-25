"""Pure-Python technical indicators.

No numpy/pandas dependency so the service stays lightweight and installs cleanly
on any Python. Each function takes a list of floats (usually closing prices) and
returns a list aligned to the input, using None for leading positions where the
indicator is not yet defined.
"""

from __future__ import annotations

from typing import List, Optional


def sma(values: List[float], period: int) -> List[Optional[float]]:
    """Simple moving average."""
    if period <= 0:
        raise ValueError("period must be positive")
    out: List[Optional[float]] = [None] * len(values)
    running = 0.0
    for i, v in enumerate(values):
        running += v
        if i >= period:
            running -= values[i - period]
        if i >= period - 1:
            out[i] = running / period
    return out


def ema(values: List[float], period: int) -> List[Optional[float]]:
    """Exponential moving average, seeded with the first SMA."""
    if period <= 0:
        raise ValueError("period must be positive")
    out: List[Optional[float]] = [None] * len(values)
    if len(values) < period:
        return out
    k = 2.0 / (period + 1.0)
    seed = sum(values[:period]) / period
    out[period - 1] = seed
    prev = seed
    for i in range(period, len(values)):
        prev = values[i] * k + prev * (1 - k)
        out[i] = prev
    return out


def rsi(values: List[float], period: int = 14) -> List[Optional[float]]:
    """Relative Strength Index (Wilder's smoothing)."""
    out: List[Optional[float]] = [None] * len(values)
    if len(values) <= period:
        return out
    gains = 0.0
    losses = 0.0
    for i in range(1, period + 1):
        delta = values[i] - values[i - 1]
        gains += max(delta, 0.0)
        losses += max(-delta, 0.0)
    avg_gain = gains / period
    avg_loss = losses / period
    out[period] = _rsi_from(avg_gain, avg_loss)
    for i in range(period + 1, len(values)):
        delta = values[i] - values[i - 1]
        gain = max(delta, 0.0)
        loss = max(-delta, 0.0)
        avg_gain = (avg_gain * (period - 1) + gain) / period
        avg_loss = (avg_loss * (period - 1) + loss) / period
        out[i] = _rsi_from(avg_gain, avg_loss)
    return out


def _rsi_from(avg_gain: float, avg_loss: float) -> float:
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def crossed_above(fast: List[Optional[float]], slow: List[Optional[float]]) -> bool:
    """True if `fast` crossed from below to above `slow` on the last bar."""
    if len(fast) < 2 or len(slow) < 2:
        return False
    f0, f1, s0, s1 = fast[-2], fast[-1], slow[-2], slow[-1]
    if None in (f0, f1, s0, s1):
        return False
    return f0 <= s0 and f1 > s1


def crossed_below(fast: List[Optional[float]], slow: List[Optional[float]]) -> bool:
    """True if `fast` crossed from above to below `slow` on the last bar."""
    if len(fast) < 2 or len(slow) < 2:
        return False
    f0, f1, s0, s1 = fast[-2], fast[-1], slow[-2], slow[-1]
    if None in (f0, f1, s0, s1):
        return False
    return f0 >= s0 and f1 < s1
