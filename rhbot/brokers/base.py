"""Broker interface. Every adapter (paper or live) implements this."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Optional

from ..models import Fill, Order


class Broker(ABC):
    """Abstraction over 'somewhere I can get a price and send an order'."""

    #: Human-readable name, e.g. "paper" / "robinhood-stock".
    name: str = "broker"

    @abstractmethod
    def get_price(self, symbol: str) -> Optional[float]:
        """Latest traded price for `symbol`, or None if unavailable."""

    @abstractmethod
    def submit(self, order: Order) -> Optional[Fill]:
        """Send `order`. Returns the resulting Fill, or None if it did not fill.

        Implementations must be idempotent-safe to call once per engine tick and
        must never raise on ordinary rejects — return None and log instead.
        """

    def is_live(self) -> bool:
        return False
