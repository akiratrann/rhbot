"""Thin client for Robinhood's OFFICIAL Crypto API (Ed25519-signed requests).

Docs: https://docs.robinhood.com/crypto/trading/
Auth: each request is signed with your Ed25519 private key. Headers:
    x-api-key, x-timestamp, x-signature (base64 of the signed message).
The signed message is:  f"{api_key}{timestamp}{path}{method}{body}"

This module is imported lazily so the rest of the service runs without the
`cryptography` package or any crypto credentials.
"""

from __future__ import annotations

import base64
import json
import logging
import time
from typing import Any, Dict, Optional

import requests

log = logging.getLogger("rhbot.crypto")

BASE_URL = "https://trading.robinhood.com"


class RobinhoodCryptoClient:
    def __init__(self, api_key: str, private_key_b64: str):
        # Imported here so missing `cryptography` doesn't break paper mode.
        from cryptography.hazmat.primitives.asymmetric import ed25519

        self.api_key = api_key
        seed = base64.b64decode(private_key_b64)
        # Robinhood provides a 32-byte Ed25519 seed (private key).
        self._priv = ed25519.Ed25519PrivateKey.from_private_bytes(seed[:32])

    # ---- signing ----------------------------------------------------------

    def _headers(self, method: str, path: str, body: str = "") -> Dict[str, str]:
        ts = str(int(time.time()))
        message = f"{self.api_key}{ts}{path}{method}{body}"
        sig = self._priv.sign(message.encode("utf-8"))
        return {
            "x-api-key": self.api_key,
            "x-timestamp": ts,
            "x-signature": base64.b64encode(sig).decode("utf-8"),
            "Content-Type": "application/json",
        }

    def _request(self, method: str, path: str,
                 body_obj: Optional[dict] = None) -> Optional[Any]:
        body = json.dumps(body_obj) if body_obj is not None else ""
        url = BASE_URL + path
        try:
            resp = requests.request(
                method, url, headers=self._headers(method, path, body),
                data=body or None, timeout=15,
            )
            if resp.status_code >= 400:
                log.error("crypto API %s %s -> %s: %s",
                          method, path, resp.status_code, resp.text[:300])
                return None
            return resp.json()
        except requests.RequestException as e:
            log.error("crypto API request failed: %s", e)
            return None

    # ---- market data ------------------------------------------------------

    def best_bid_ask(self, symbol: str) -> Optional[dict]:
        path = f"/api/v1/crypto/marketdata/best_bid_ask/?symbol={symbol}"
        data = self._request("GET", path)
        if not data:
            return None
        results = data.get("results") or []
        return results[0] if results else None

    def get_price(self, symbol: str) -> Optional[float]:
        quote = self.best_bid_ask(symbol)
        if not quote:
            return None
        try:
            bid = float(quote["bid_price"])
            ask = float(quote["ask_price"])
            return (bid + ask) / 2.0
        except (KeyError, TypeError, ValueError):
            return None

    # ---- trading ----------------------------------------------------------

    def place_market_order(self, symbol: str, side: str,
                           asset_quantity: float,
                           client_order_id: str) -> Optional[dict]:
        """Place a market order. `side` is 'buy' or 'sell'."""
        path = "/api/v1/crypto/trading/orders/"
        body = {
            "client_order_id": client_order_id,
            "side": side,
            "type": "market",
            "symbol": symbol,
            "market_order_config": {"asset_quantity": str(asset_quantity)},
        }
        return self._request("POST", path, body)
