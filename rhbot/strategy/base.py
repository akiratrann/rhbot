"""Strategy interface.

A strategy is a PURE function of market data + current position -> Signal.
Keep it deterministic and side-effect free: no network calls, no order placement.
The engine turns your Signal into a risk-checked order. That separation is what
keeps an unattended bot predictable.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import List, Optional

from ..models import Bar, Position, Signal


class Strategy(ABC):
    #: Minimum number of bars required before this strategy can emit a signal.
    warmup_bars: int = 2

    def __init__(self, params: Optional[dict] = None):
        self.params = params or {}

    @abstractmethod
    def evaluate(self, symbol: str, bars: List[Bar],
                 position: Optional[Position]) -> Signal:
        """Return a Signal for `symbol` given recent `bars` (oldest first) and
        the current `position` (None if flat)."""
