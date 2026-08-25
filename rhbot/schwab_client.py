"""Client for the OFFICIAL Charles Schwab Trader API (OAuth 2.0).

Docs: https://developer.schwab.com

Two things about this API shape the whole module:

  * The **refresh token expires after 7 days** and cannot be renewed
    programmatically. When it dies the service stops trading and tells you to
    run `python schwab_login.py`. That is a Schwab security policy, not
    something this code can work around.
  * The **access token lasts 30 minutes** and IS renewed automatically from the
    refresh token, so within a week the bot runs unattended.

Tokens live in `state/schwab_token.json` (mode 0600). Treat that file like a
password — anyone holding it can trade your account until it expires.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import time
from typing import Any, Dict, List, Optional
from urllib.parse import urlencode

import requests

log = logging.getLogger("rhbot.schwab")

AUTH_URL = "https://api.schwabapi.com/v1/oauth/authorize"
TOKEN_URL = "https://api.schwabapi.com/v1/oauth/token"
BASE_URL = "https://api.schwabapi.com"

#: Schwab's hard ceiling on a refresh token, in seconds.
REFRESH_TOKEN_LIFETIME = 7 * 24 * 3600
#: Renew the access token this many seconds before it actually expires.
ACCESS_TOKEN_SKEW = 60
#: Start nagging about re-authentication when the refresh token has less left.
REAUTH_WARNING_SECONDS = 24 * 3600

DEFAULT_TOKEN_PATH = "state/schwab_token.json"

#: rhbot bar_interval -> Schwab price-history query params.
#: Schwab has no native hourly candle, so 1h is aggregated from 30m (see
#: SchwabFeed). Everything else maps directly.
PRICE_HISTORY = {
    "1m": {"periodType": "day", "period": 10,
           "frequencyType": "minute", "frequency": 1},
    "5m": {"periodType": "day", "period": 10,
           "frequencyType": "minute", "frequency": 5},
    "15m": {"periodType": "day", "period": 10,
            "frequencyType": "minute", "frequency": 15},
    "1h": {"periodType": "day", "period": 10,
           "frequencyType": "minute", "frequency": 30},
    "1d": {"periodType": "year", "period": 2,
           "frequencyType": "daily", "frequency": 1},
}


class SchwabAuthError(RuntimeError):
    """Raised when the refresh token is dead and only a human can fix it."""


def build_authorize_url(app_key: str, callback_url: str) -> str:
    return f"{AUTH_URL}?" + urlencode({
        "client_id": app_key,
        "redirect_uri": callback_url,
        "response_type": "code",
    })


def _basic_auth(app_key: str, app_secret: str) -> str:
    raw = f"{app_key}:{app_secret}".encode("utf-8")
    return "Basic " + base64.b64encode(raw).decode("utf-8")


def exchange_code_for_tokens(app_key: str, app_secret: str, code: str,
                             callback_url: str,
                             token_path: str = DEFAULT_TOKEN_PATH) -> dict:
    """Complete the initial OAuth handshake and persist the tokens.

    Called by `schwab_login.py`, not by the running service — the service can
    only ever refresh, never authorize.
    """
    resp = requests.post(
        TOKEN_URL,
        headers={"Authorization": _basic_auth(app_key, app_secret),
                 "Content-Type": "application/x-www-form-urlencoded"},
        data={"grant_type": "authorization_code", "code": code,
              "redirect_uri": callback_url},
        timeout=30,
    )
    if resp.status_code >= 400:
        raise SchwabAuthError(
            f"token exchange failed ({resp.status_code}): {resp.text[:300]}"
        )
    payload = resp.json()
    now = time.time()
    tokens = {
        "access_token": payload["access_token"],
        "refresh_token": payload["refresh_token"],
        "access_expires_at": now + float(payload.get("expires_in", 1800)),
        # The 7-day clock starts HERE and is never extended by a refresh.
        "refresh_expires_at": now + REFRESH_TOKEN_LIFETIME,
    }
    save_tokens(tokens, token_path)
    return tokens


def save_tokens(tokens: dict, path: str = DEFAULT_TOKEN_PATH) -> None:
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(tokens, f, indent=2)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def load_tokens(path: str = DEFAULT_TOKEN_PATH) -> Optional[dict]:
    try:
        with open(path, "r") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


class SchwabClient:
    def __init__(self, app_key: str, app_secret: str,
                 token_path: str = DEFAULT_TOKEN_PATH, timeout: int = 20):
        self.app_key = app_key
        self.app_secret = app_secret
        self.token_path = token_path
        self.timeout = timeout
        self._session = requests.Session()
        self._tokens = load_tokens(token_path)
        self._account_hash: Optional[str] = None
        if not self._tokens:
            raise SchwabAuthError(
                f"No Schwab tokens at {token_path}. Run:  python schwab_login.py"
            )

    # ---- token lifecycle --------------------------------------------------

    def refresh_seconds_left(self) -> float:
        return self._tokens.get("refresh_expires_at", 0) - time.time()

    def _ensure_access_token(self) -> str:
        """Return a valid access token, refreshing it if it is near expiry."""
        if self.refresh_seconds_left() <= 0:
            raise SchwabAuthError(
                "Schwab refresh token has EXPIRED (they last 7 days and cannot "
                "be renewed programmatically). Run:  python schwab_login.py"
            )

        expires_at = self._tokens.get("access_expires_at", 0)
        if time.time() < expires_at - ACCESS_TOKEN_SKEW:
            return self._tokens["access_token"]

        resp = self._session.post(
            TOKEN_URL,
            headers={"Authorization": _basic_auth(self.app_key, self.app_secret),
                     "Content-Type": "application/x-www-form-urlencoded"},
            data={"grant_type": "refresh_token",
                  "refresh_token": self._tokens["refresh_token"]},
            timeout=self.timeout,
        )
        if resp.status_code >= 400:
            raise SchwabAuthError(
                f"access-token refresh failed ({resp.status_code}): "
                f"{resp.text[:200]}. Run:  python schwab_login.py"
            )
        payload = resp.json()
        self._tokens["access_token"] = payload["access_token"]
        self._tokens["access_expires_at"] = (
            time.time() + float(payload.get("expires_in", 1800))
        )
        # Schwab hands back a refresh token on refresh, but the 7-day clock is
        # NOT extended — keep the original deadline so we warn on time.
        if payload.get("refresh_token"):
            self._tokens["refresh_token"] = payload["refresh_token"]
        save_tokens(self._tokens, self.token_path)

        left = self.refresh_seconds_left()
        if left < REAUTH_WARNING_SECONDS:
            log.warning("Schwab refresh token expires in %.1f hours — run "
                        "`python schwab_login.py` before then or trading stops.",
                        left / 3600)
        return self._tokens["access_token"]

    # ---- plumbing ---------------------------------------------------------

    def _request(self, method: str, path: str,
                 params: Optional[dict] = None,
                 json_body: Optional[dict] = None) -> Optional[Any]:
        """Returns parsed JSON (or a dict with `_location` for 201s).

        Auth failures raise SchwabAuthError because only a human can fix them;
        everything else logs and returns None so the trading loop survives.
        """
        try:
            token = self._ensure_access_token()
        except SchwabAuthError:
            raise
        except Exception as e:  # noqa: BLE001
            log.error("schwab: could not obtain an access token: %s", e)
            return None

        try:
            resp = self._session.request(
                method, BASE_URL + path,
                headers={"Authorization": f"Bearer {token}",
                         "Accept": "application/json"},
                params=params, json=json_body, timeout=self.timeout,
            )
        except requests.RequestException as e:
            log.error("schwab %s %s failed: %s", method, path, e)
            return None

        if resp.status_code == 401:
            raise SchwabAuthError(
                "Schwab rejected the access token (401). Run: "
                "python schwab_login.py"
            )
        if resp.status_code >= 400:
            log.error("schwab %s %s -> %s: %s",
                      method, path, resp.status_code, resp.text[:300])
            return None

        # Order placement returns 201 with an empty body; the new order id is
        # only available as the last path segment of the Location header.
        if resp.status_code == 201:
            location = resp.headers.get("Location", "")
            return {"_location": location,
                    "order_id": location.rstrip("/").split("/")[-1] or None}
        if resp.status_code == 204 or not resp.content:
            return {}
        try:
            return resp.json()
        except ValueError:
            log.error("schwab %s %s -> non-JSON body", method, path)
            return None

    # ---- accounts ---------------------------------------------------------

    def account_hash(self) -> Optional[str]:
        """Schwab addresses accounts by an opaque hash, not the account number."""
        if self._account_hash:
            return self._account_hash
        data = self._request("GET", "/trader/v1/accounts/accountNumbers")
        if not data:
            return None
        try:
            self._account_hash = data[0]["hashValue"]
        except (IndexError, KeyError, TypeError):
            log.error("schwab: no accounts returned for these credentials")
            return None
        if len(data) > 1:
            log.warning("schwab: %d accounts found, using the first (%s). "
                        "Set SCHWAB_ACCOUNT_NUMBER to pick another.",
                        len(data), data[0].get("accountNumber"))
        return self._account_hash

    def select_account(self, account_number: str) -> bool:
        """Pin trading to a specific account number instead of the first one."""
        data = self._request("GET", "/trader/v1/accounts/accountNumbers") or []
        for entry in data:
            if str(entry.get("accountNumber")) == str(account_number):
                self._account_hash = entry["hashValue"]
                return True
        log.error("schwab: account %s not found on these credentials",
                  account_number)
        return False

    def get_account(self, with_positions: bool = False) -> Optional[dict]:
        acct_hash = self.account_hash()
        if not acct_hash:
            return None
        params = {"fields": "positions"} if with_positions else None
        data = self._request("GET", f"/trader/v1/accounts/{acct_hash}",
                             params=params)
        if not data:
            return None
        return data.get("securitiesAccount", data)

    # ---- market data ------------------------------------------------------

    def get_quote(self, symbol: str) -> Optional[float]:
        data = self._request("GET", "/marketdata/v1/quotes",
                             params={"symbols": symbol})
        quote = ((data or {}).get(symbol) or {}).get("quote") or {}
        for key in ("lastPrice", "mark", "closePrice"):
            try:
                value = float(quote[key])
            except (KeyError, TypeError, ValueError):
                continue
            if value > 0:
                return value
        return None

    def get_candles(self, symbol: str, interval: str,
                    ) -> List[dict]:
        """Raw Schwab candles (open/high/low/close/volume/datetime ms)."""
        spec = PRICE_HISTORY.get(interval)
        if not spec:
            log.error("schwab: unsupported bar interval %r", interval)
            return []
        params = dict(spec)
        params["symbol"] = symbol
        params["needExtendedHoursData"] = "false"
        data = self._request("GET", "/marketdata/v1/pricehistory", params=params)
        if not data or data.get("empty"):
            return []
        return data.get("candles") or []

    # ---- trading ----------------------------------------------------------

    def place_equity_market_order(self, symbol: str, instruction: str,
                                  quantity: int) -> Optional[str]:
        """Place a whole-share market order. Returns the order id, or None.

        Schwab has no notional/fractional order type on this API, so callers
        must convert dollars to whole shares before getting here.
        """
        acct_hash = self.account_hash()
        if not acct_hash:
            return None
        body = {
            "orderType": "MARKET",
            "session": "NORMAL",
            "duration": "DAY",
            "orderStrategyType": "SINGLE",
            "orderLegCollection": [{
                "instruction": instruction,
                "quantity": quantity,
                "instrument": {"symbol": symbol, "assetType": "EQUITY"},
            }],
        }
        resp = self._request("POST", f"/trader/v1/accounts/{acct_hash}/orders",
                             json_body=body)
        return (resp or {}).get("order_id")

    def get_order(self, order_id: str) -> Optional[dict]:
        acct_hash = self.account_hash()
        if not acct_hash:
            return None
        return self._request(
            "GET", f"/trader/v1/accounts/{acct_hash}/orders/{order_id}")

    def cancel_order(self, order_id: str) -> bool:
        acct_hash = self.account_hash()
        if not acct_hash:
            return False
        return self._request(
            "DELETE",
            f"/trader/v1/accounts/{acct_hash}/orders/{order_id}") is not None

    def await_fill(self, order_id: str, timeout_seconds: float = 10.0,
                   poll_seconds: float = 0.5,
                   sleep=time.sleep) -> Optional[dict]:
        """Poll until the order is terminal; cancel it if it never gets there.

        Leaving a working order behind after the engine has moved on is how the
        local book silently drifts away from the account.
        """
        terminal = {"FILLED", "CANCELED", "REJECTED", "EXPIRED", "REPLACED"}
        deadline = time.monotonic() + timeout_seconds
        order = None
        while time.monotonic() < deadline:
            order = self.get_order(order_id)
            if not order:
                return None
            if str(order.get("status", "")).upper() in terminal:
                return order
            sleep(poll_seconds)

        log.warning("schwab order %s still %s after %.0fs — cancelling",
                    order_id, (order or {}).get("status"), timeout_seconds)
        self.cancel_order(order_id)
        return self.get_order(order_id) or order


def average_fill_price(order: dict) -> tuple:
    """(filled_quantity, weighted average price) from a Schwab order.

    Schwab reports each partial execution separately, so a multi-leg fill has to
    be averaged by quantity — taking the last price would misreport the basis.
    """
    total_qty = 0.0
    total_cost = 0.0
    for activity in order.get("orderActivityCollection") or []:
        for leg in activity.get("executionLegs") or []:
            try:
                qty = float(leg["quantity"])
                price = float(leg["price"])
            except (KeyError, TypeError, ValueError):
                continue
            total_qty += qty
            total_cost += qty * price

    if total_qty > 0:
        return total_qty, total_cost / total_qty

    # Fall back to the summary fields when execution detail is absent.
    try:
        qty = float(order.get("filledQuantity") or 0)
    except (TypeError, ValueError):
        qty = 0.0
    try:
        price = float(order.get("price") or 0)
    except (TypeError, ValueError):
        price = 0.0
    return qty, price
