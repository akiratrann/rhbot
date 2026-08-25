"""Thin client for Alpaca's OFFICIAL REST API (market data + trading).

Docs: https://docs.alpaca.markets/reference/
Auth: two static headers, no signing and no login flow — which is why this is
far less fragile than the unofficial `robin_stocks` path.

    APCA-API-KEY-ID / APCA-API-SECRET-KEY

There are two independent hosts:
  * TRADING  — paper or live, chosen by `paper=`. The paper host is a REAL
    order engine with fake money: same endpoints, same fill semantics, no PDT
    limit and no settlement. That is the whole reason to prefer it while
    developing.
  * DATA     — one host for both, gated by your data subscription. The free
    tier serves IEX for stocks (real-time, thinner book) and full crypto.

Only `requests` is needed, so this imports cleanly with no extra dependency.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import requests

log = logging.getLogger("rhbot.alpaca")

TRADING_PAPER = "https://paper-api.alpaca.markets"
TRADING_LIVE = "https://api.alpaca.markets"
DATA_URL = "https://data.alpaca.markets"

#: rhbot bar_interval -> Alpaca timeframe string.
TIMEFRAMES = {
    "1m": "1Min",
    "5m": "5Min",
    "15m": "15Min",
    "1h": "1Hour",
    "1d": "1Day",
}


#: Minutes covered by each Alpaca timeframe string.
_TIMEFRAME_MINUTES = {
    "1Min": 1, "5Min": 5, "15Min": 15, "30Min": 30, "1Hour": 60, "1Day": 1440,
}

#: Minutes in a regular US equities session.
_SESSION_MINUTES = 390


#: Quote currencies Alpaca concatenates onto crypto pairs. Longest first, so
#: `ETHUSDT` splits as ETH/USDT rather than ETH/USD + a stray T.
_CRYPTO_QUOTES = ("USDT", "USDC", "USD", "BTC", "ETH")


def to_alpaca_symbol(symbol: str, is_crypto: bool) -> str:
    """rhbot form -> the form the DATA and ORDERS endpoints want.

    Alpaca spells one crypto asset three different ways and they are not
    interchangeable:
        data / orders   BTC/USD
        positions       BTCUSD      (see `position_symbol`)
        rhbot           BTC-USD
    """
    return symbol.replace("-", "/") if is_crypto else symbol


def position_symbol(symbol: str, is_crypto: bool) -> str:
    """rhbot form -> the form the POSITIONS endpoint wants.

    `GET /v2/positions/BTC/USD` 404s — the slash reads as a path separator.
    The concatenated `BTCUSD` is the only form that resolves.
    """
    return symbol.replace("-", "") if is_crypto else symbol


def from_alpaca_symbol(symbol: str, is_crypto: bool) -> str:
    """Any Alpaca spelling -> rhbot form. Inverse of the two above.

    Without this, a crypto position read back from the broker never matches the
    local book, so the drift check false-alarms on every crypto holding.
    """
    if not is_crypto:
        return symbol
    if "/" in symbol:
        return symbol.replace("/", "-")
    if "-" in symbol:
        return symbol
    upper = symbol.upper()
    for quote in _CRYPTO_QUOTES:
        if upper.endswith(quote) and len(upper) > len(quote):
            return f"{upper[:-len(quote)]}-{quote}"
    return symbol


def lookback_start(timeframe: str, limit: int, is_crypto: bool,
                   now: Optional[datetime] = None) -> str:
    """How far back to ask for, to actually receive `limit` bars.

    Without an explicit `start` the bars endpoint returns only the most recent
    bar, which silently starves every strategy of its warmup history. The span
    is padded for weekends and holidays; over-fetching is free because `limit`
    caps the response anyway.
    """
    minutes = _TIMEFRAME_MINUTES.get(timeframe, 1440)
    if is_crypto:
        days = limit * minutes / 1440 + 1  # 24/7 at every timeframe, no gaps
    elif minutes >= 1440:
        days = limit * 1.6 + 10          # ~5 sessions per 7 calendar days
    else:
        bars_per_session = max(1.0, _SESSION_MINUTES / minutes)
        days = limit / bars_per_session * 1.6 + 5
    start = (now or datetime.now(timezone.utc)) - timedelta(days=days)
    return start.strftime("%Y-%m-%dT%H:%M:%SZ")


class AlpacaClient:
    def __init__(self, api_key: str, api_secret: str, paper: bool = True,
                 data_feed: str = "iex", timeout: int = 20):
        self.paper = paper
        self.trading_url = TRADING_PAPER if paper else TRADING_LIVE
        self.data_feed = data_feed
        self.timeout = timeout
        self._clock_cache: Optional[tuple] = None
        self._session = requests.Session()
        self._session.headers.update({
            "APCA-API-KEY-ID": api_key,
            "APCA-API-SECRET-KEY": api_secret,
            "Accept": "application/json",
        })

    # ---- plumbing ---------------------------------------------------------

    def _request(self, method: str, url: str,
                 params: Optional[dict] = None,
                 json_body: Optional[dict] = None) -> Optional[Any]:
        """Returns parsed JSON, or None on any error. Never raises."""
        try:
            resp = self._session.request(method, url, params=params,
                                         json=json_body, timeout=self.timeout)
        except requests.RequestException as e:
            log.error("alpaca %s %s failed: %s", method, url, e)
            return None

        if resp.status_code >= 400:
            log.error("alpaca %s %s -> %s: %s",
                      method, url, resp.status_code, resp.text[:300])
            return None
        if resp.status_code == 204 or not resp.content:
            return {}
        try:
            return resp.json()
        except ValueError:
            log.error("alpaca %s %s -> non-JSON body", method, url)
            return None

    def _trade(self, method: str, path: str, **kw) -> Optional[Any]:
        return self._request(method, self.trading_url + path, **kw)

    def _data(self, path: str, params: dict) -> Optional[Any]:
        return self._request("GET", DATA_URL + path, params=params)

    # ---- account ----------------------------------------------------------

    def get_account(self) -> Optional[dict]:
        return self._trade("GET", "/v2/account")

    def is_market_open(self, cache_seconds: float = 60.0) -> Optional[bool]:
        """Equities session state, cached so it isn't refetched per order.

        Returns None when the clock can't be reached — callers must treat that
        as "unknown", not "closed", or a network blip silently halts trading.
        """
        now = time.monotonic()
        if self._clock_cache and (now - self._clock_cache[0]) < cache_seconds:
            return self._clock_cache[1]
        clock = self._trade("GET", "/v2/clock")
        if not clock:
            return None
        is_open = bool(clock.get("is_open"))
        self._clock_cache = (now, is_open)
        return is_open

    # ---- market data ------------------------------------------------------

    def get_bars(self, symbol: str, timeframe: str, limit: int,
                 is_crypto: bool) -> List[dict]:
        """Raw Alpaca bar dicts (keys: t, o, h, l, c, v), oldest first.

        Two non-obvious query params are load-bearing:

          * Without `start`, the endpoint returns only the single most recent
            bar, which starves every strategy of its warmup history.
          * Without `sort=desc`, it returns bars ASCENDING from `start` and
            `limit` truncates the NEWEST ones — you get a window that silently
            ends weeks ago. Descending takes the most recent `limit` bars, so
            the result is reversed here to restore oldest-first.
        """
        sym = to_alpaca_symbol(symbol, is_crypto)
        params = {"symbols": sym, "timeframe": timeframe, "limit": limit,
                  "sort": "desc",
                  "start": lookback_start(timeframe, limit, is_crypto)}
        if is_crypto:
            path = "/v1beta3/crypto/us/bars"
        else:
            path = "/v2/stocks/bars"
            params.update({"feed": self.data_feed, "adjustment": "raw"})
        data = self._data(path, params)
        if not data:
            return []
        return list(reversed((data.get("bars") or {}).get(sym) or []))

    def get_price(self, symbol: str, is_crypto: bool) -> Optional[float]:
        """Latest price: crypto uses the bid/ask midpoint, stocks last trade."""
        sym = to_alpaca_symbol(symbol, is_crypto)
        if is_crypto:
            data = self._data("/v1beta3/crypto/us/latest/quotes",
                              {"symbols": sym})
            quote = ((data or {}).get("quotes") or {}).get(sym)
            if not quote:
                return None
            try:
                bid, ask = float(quote["bp"]), float(quote["ap"])
            except (KeyError, TypeError, ValueError):
                return None
            # A one-sided book gives a zero on the missing side; don't average
            # that in or the mid comes out half price.
            if bid > 0 and ask > 0:
                return (bid + ask) / 2.0
            return bid or ask or None

        data = self._data("/v2/stocks/trades/latest",
                          {"symbols": sym, "feed": self.data_feed})
        trade = ((data or {}).get("trades") or {}).get(sym)
        try:
            return float(trade["p"]) if trade else None
        except (KeyError, TypeError, ValueError):
            return None

    # ---- trading ----------------------------------------------------------

    def submit_market_order(self, symbol: str, side: str, is_crypto: bool,
                            notional: Optional[float] = None,
                            qty: Optional[float] = None,
                            client_order_id: Optional[str] = None
                            ) -> Optional[dict]:
        """Place a market order. Supply exactly one of `notional` or `qty`.

        Crypto is GTC because it trades 24/7 and a `day` order would expire at
        the equities session boundary.
        """
        body: Dict[str, Any] = {
            "symbol": to_alpaca_symbol(symbol, is_crypto),
            "side": side,
            "type": "market",
            "time_in_force": "gtc" if is_crypto else "day",
        }
        if notional is not None:
            body["notional"] = str(round(notional, 2))
        elif qty is not None:
            body["qty"] = str(qty)
        else:
            raise ValueError("submit_market_order needs notional or qty")
        if client_order_id:
            body["client_order_id"] = client_order_id
        return self._trade("POST", "/v2/orders", json_body=body)

    def get_order(self, order_id: str) -> Optional[dict]:
        return self._trade("GET", f"/v2/orders/{order_id}")

    def get_position(self, symbol: str, is_crypto: bool) -> Optional[dict]:
        """Open position for `symbol`, or None when flat (Alpaca 404s)."""
        sym = position_symbol(symbol, is_crypto)
        return self._trade("GET", f"/v2/positions/{sym}")

    def list_positions(self) -> List[dict]:
        return self._trade("GET", "/v2/positions") or []

    def cancel_order(self, order_id: str) -> bool:
        return self._trade("DELETE", f"/v2/orders/{order_id}") is not None

    def await_fill(self, order_id: str, timeout_seconds: float = 8.0,
                   poll_seconds: float = 0.5,
                   sleep=time.sleep) -> Optional[dict]:
        """Poll until the order reaches a terminal state; return the order dict.

        On timeout the order is CANCELLED. That matters: leaving a live order
        behind that the engine has already given up on is how the local
        portfolio silently drifts out of sync with the real account. A partial
        fill is still reported back so the books stay correct.
        """
        deadline = time.monotonic() + timeout_seconds
        order = None
        while time.monotonic() < deadline:
            order = self.get_order(order_id)
            if not order:
                return None
            status = order.get("status")
            if status in ("filled", "canceled", "expired", "rejected"):
                return order
            sleep(poll_seconds)

        log.warning("alpaca order %s still %s after %.0fs — cancelling",
                    order_id, (order or {}).get("status"), timeout_seconds)
        self.cancel_order(order_id)
        return self.get_order(order_id) or order
