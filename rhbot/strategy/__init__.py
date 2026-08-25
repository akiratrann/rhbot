"""Strategies. Add your own by subclassing `Strategy` and registering it."""

from .base import Strategy
from .sma_crossover import SmaCrossover
from .slope_reversal import SlopeReversal
from .swing_trend import SwingTrend
from .trend_follow import TrendFollow

# Name -> class. Reference these names in config.yaml's `strategy:` field.
REGISTRY = {
    "sma_crossover": SmaCrossover,
    "slope_reversal": SlopeReversal,
    "swing_trend": SwingTrend,
    "trend_follow": TrendFollow,
}


def build_strategy(name: str, params: dict) -> Strategy:
    if name not in REGISTRY:
        raise ValueError(
            f"unknown strategy {name!r}. Known: {sorted(REGISTRY)}"
        )
    return REGISTRY[name](params)
