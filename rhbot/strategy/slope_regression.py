"""Slope reversal using a least-squares derivative instead of a difference.

Same idea as `slope_reversal` — buy when the slope turns up, sell when it turns
down — but the slope is estimated properly.

WHY THE DERIVATIVE ESTIMATOR MATTERS
------------------------------------
`slope_reversal` computes the slope as `smoothed[-1] - smoothed[-2]`: a
two-point difference. That is the noisiest possible estimator of a derivative.
Differencing amplifies noise — if each close carries independent noise of size
s, their difference carries noise of size s*sqrt(2), while the *signal* you are
trying to measure is only one bar's worth of drift. The finer the bars, the
worse this gets, which is exactly why 1-minute bars performed worse than daily
ones rather than better.

A least-squares fit over `window` bars uses every point to estimate one slope.
Its noise falls off roughly as 1/window^1.5, so a 10-bar regression is far
steadier than a 2-point difference without adding the lag that heavy smoothing
costs you. This is the standard reason regression slope is preferred to naive
differencing on noisy series.

The second derivative (curvature) is available from the same fit and is used as
a confirmation: a slope crossing zero while curvature is flat is usually noise,
whereas a genuine turn has the slope crossing zero WITH curvature pushing it
through. `require_curvature` turns that filter on.

TEMPLATE, not advice. Backtest before trusting it.
"""

from __future__ import annotations

from typing import List, Optional, Tuple

from ..models import Bar, Position, Signal, SignalType
from .base import Strategy


def regression_slope(values: List[float]) -> Optional[float]:
    """Least-squares slope of `values` against 0..n-1, in units per bar.

    Closed form rather than a matrix solve: x is always 0..n-1, so the
    denominator is a constant that depends only on n.
    """
    n = len(values)
    if n < 2:
        return None
    mean_x = (n - 1) / 2.0
    mean_y = sum(values) / n
    num = sum((i - mean_x) * (v - mean_y) for i, v in enumerate(values))
    den = sum((i - mean_x) ** 2 for i in range(n))
    return num / den if den else None


def slope_series(closes: List[float], window: int) -> List[Optional[float]]:
    """Rolling regression slope, normalised to percent of price per bar.

    Normalising matters: a $2/bar slope means something entirely different on a
    $40 stock than on a $78,000 one, and the deadband is expressed in percent.
    """
    out: List[Optional[float]] = [None] * len(closes)
    for i in range(window - 1, len(closes)):
        seg = closes[i - window + 1: i + 1]
        s = regression_slope(seg)
        ref = seg[-1]
        out[i] = (s / ref) if (s is not None and ref) else None
    return out


class SlopeRegression(Strategy):
    def __init__(self, params: Optional[dict] = None):
        super().__init__(params)
        self.window = int(self.params.get("window", 10))
        #: Deadband, as a fraction of price per bar. Flips smaller than this
        #: are treated as noise rather than turns.
        self.min_slope_pct = float(self.params.get("min_slope_pct", 0.0))
        #: Require curvature to agree with the turn (see module docstring).
        self.require_curvature = bool(self.params.get("require_curvature", False))

        if self.window < 3:
            raise ValueError(f"window must be >= 3, got {self.window}")
        # +2 so there is a previous slope to compare against, +1 for curvature.
        self.warmup_bars = self.window + 3

    def _slopes(self, closes: List[float]) -> Tuple[Optional[float], Optional[float],
                                                    Optional[float]]:
        s = slope_series(closes, self.window)
        return s[-1], s[-2], s[-3] if len(s) >= 3 else None

    def evaluate(self, symbol: str, bars: List[Bar],
                 position: Optional[Position]) -> Signal:
        if len(bars) < self.warmup_bars:
            return Signal(symbol, SignalType.HOLD, "warming up")

        closes = [b.close for b in bars]
        now, prev, prev2 = self._slopes(closes)
        if now is None or prev is None:
            return Signal(symbol, SignalType.HOLD, "slope not ready")

        holding = position is not None and position.quantity > 0
        band = self.min_slope_pct

        # Curvature = change in slope. A turn confirmed by curvature is one
        # where the slope is not just crossing zero but being pushed through.
        curvature = (now - prev) if prev is not None else 0.0

        if not holding:
            turned_up = prev <= 0 < now and now >= band
            if turned_up and (not self.require_curvature or curvature > 0):
                return Signal(symbol, SignalType.ENTER_LONG,
                              f"regression slope crossed up "
                              f"({prev:+.4%} -> {now:+.4%}/bar over "
                              f"{self.window} bars)")
            return Signal(symbol, SignalType.HOLD, "no upward crossing")

        turned_down = prev >= 0 > now and now <= -band
        if turned_down and (not self.require_curvature or curvature < 0):
            return Signal(symbol, SignalType.EXIT_LONG,
                          f"regression slope crossed down "
                          f"({prev:+.4%} -> {now:+.4%}/bar)")
        return Signal(symbol, SignalType.HOLD, "slope still up")
