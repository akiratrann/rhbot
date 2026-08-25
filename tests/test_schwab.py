"""Tests for the Schwab adapter.

Schwab is live-money-only, so these lean hard on the two places a bug is
expensive: whole-share sizing (Schwab has no fractional orders) and the guard
rails that stop `mode: paper` from ever reaching this code path.

Everything runs against a fake HTTP session — no network, no keys, no orders.
"""

from __future__ import annotations

import json
import time

import pytest

from rhbot.brokers.schwab import SchwabBroker
from rhbot.config import Config, RiskConfig, Secrets, WatchItem, _validate
from rhbot.data.schwab_feed import SchwabFeed, _pair_into_hours
from rhbot.factory import resolve_broker
from rhbot.models import AssetClass, Bar, Order, Side
from rhbot.schwab_client import (REFRESH_TOKEN_LIFETIME, SchwabAuthError,
                                 SchwabClient, average_fill_price,
                                 build_authorize_url, load_tokens)

from datetime import datetime, timezone


# ---- fake transport -------------------------------------------------------

class FakeResponse:
    def __init__(self, payload=None, status_code=200, headers=None):
        self._payload = payload if payload is not None else {}
        self.status_code = status_code
        self.headers = headers or {}
        self.content = b"{}" if payload is not None else b""
        self.text = json.dumps(self._payload)

    def json(self):
        return self._payload


class FakeSession:
    """Routes on (METHOD, url-substring); a list value is consumed in order."""

    def __init__(self, routes):
        self.routes = list(routes)
        self.calls = []

    def request(self, method, url, headers=None, params=None, json=None,
                timeout=None):
        self.calls.append({"method": method, "url": url, "params": params,
                           "json": json})
        for m, frag, resp in self.routes:
            if m == method and frag in url:
                if isinstance(resp, list):
                    return resp.pop(0) if len(resp) > 1 else resp[0]
                return resp
        return FakeResponse({}, status_code=404)

    def post(self, url, headers=None, data=None, timeout=None):
        self.calls.append({"method": "POST", "url": url, "data": data})
        for m, frag, resp in self.routes:
            if m == "POST" and frag in url:
                return resp
        return FakeResponse({}, status_code=404)

    def sent_bodies(self, frag):
        return [c["json"] for c in self.calls
                if c.get("json") and frag in c["url"]]


ACCOUNTS = [{"accountNumber": "12345678", "hashValue": "HASH1"}]


def make_client(routes, tmp_path, refresh_left=REFRESH_TOKEN_LIFETIME):
    token_path = str(tmp_path / "tok.json")
    with open(token_path, "w") as f:
        json.dump({"access_token": "at", "refresh_token": "rt",
                   "access_expires_at": time.time() + 1800,
                   "refresh_expires_at": time.time() + refresh_left}, f)
    client = SchwabClient("key", "secret", token_path=token_path)
    routes = list(routes) + [("GET", "accountNumbers", FakeResponse(ACCOUNTS))]
    client._session = FakeSession(routes)
    return client


# ---- token lifecycle ------------------------------------------------------

def test_missing_token_file_is_a_clear_error(tmp_path):
    with pytest.raises(SchwabAuthError, match="schwab_login.py"):
        SchwabClient("k", "s", token_path=str(tmp_path / "nope.json"))


def test_expired_refresh_token_refuses_to_trade(tmp_path):
    client = make_client([], tmp_path, refresh_left=-1)
    with pytest.raises(SchwabAuthError, match="EXPIRED"):
        client.get_quote("AAPL")


def test_access_token_is_refreshed_when_stale(tmp_path):
    client = make_client([
        ("POST", "/oauth/token",
         FakeResponse({"access_token": "new", "expires_in": 1800})),
        ("GET", "/marketdata/v1/quotes",
         FakeResponse({"AAPL": {"quote": {"lastPrice": 100.0}}})),
    ], tmp_path)
    client._tokens["access_expires_at"] = time.time() - 1  # force a refresh

    assert client.get_quote("AAPL") == pytest.approx(100.0)
    assert client._tokens["access_token"] == "new"


def test_refresh_does_not_extend_the_seven_day_clock(tmp_path):
    """Schwab resets the access token, never the 7-day refresh deadline."""
    client = make_client([
        ("POST", "/oauth/token",
         FakeResponse({"access_token": "new", "expires_in": 1800,
                       "refresh_token": "rt2"})),
        ("GET", "/marketdata/v1/quotes",
         FakeResponse({"AAPL": {"quote": {"lastPrice": 100.0}}})),
    ], tmp_path, refresh_left=3600)
    before = client._tokens["refresh_expires_at"]
    client._tokens["access_expires_at"] = time.time() - 1
    client.get_quote("AAPL")
    assert client._tokens["refresh_expires_at"] == before


def test_refreshed_tokens_are_persisted(tmp_path):
    client = make_client([
        ("POST", "/oauth/token",
         FakeResponse({"access_token": "new", "expires_in": 1800})),
        ("GET", "/marketdata/v1/quotes",
         FakeResponse({"AAPL": {"quote": {"lastPrice": 1.0}}})),
    ], tmp_path)
    client._tokens["access_expires_at"] = time.time() - 1
    client.get_quote("AAPL")
    assert load_tokens(client.token_path)["access_token"] == "new"


def test_401_demands_reauthentication(tmp_path):
    client = make_client([("GET", "/marketdata/v1/quotes",
                           FakeResponse({}, status_code=401))], tmp_path)
    with pytest.raises(SchwabAuthError, match="schwab_login.py"):
        client.get_quote("AAPL")


def test_authorize_url_carries_key_and_callback():
    url = build_authorize_url("APPKEY", "https://127.0.0.1")
    assert "client_id=APPKEY" in url
    assert "response_type=code" in url


# ---- market data ----------------------------------------------------------

def test_quote_falls_back_when_last_price_is_absent(tmp_path):
    client = make_client([("GET", "/marketdata/v1/quotes",
                           FakeResponse({"AAPL": {"quote": {"mark": 55.5}}}))],
                         tmp_path)
    assert client.get_quote("AAPL") == pytest.approx(55.5)


def test_quote_returns_none_when_empty(tmp_path):
    client = make_client([("GET", "/marketdata/v1/quotes",
                           FakeResponse({}))], tmp_path)
    assert client.get_quote("AAPL") is None


CANDLES = {"candles": [
    {"open": 10, "high": 12, "low": 9, "close": 11, "volume": 100,
     "datetime": 1754000000000},
    {"open": 11, "high": 14, "low": 11, "close": 13, "volume": 150,
     "datetime": 1754086400000},
], "empty": False}


def test_candles_become_bars(tmp_path):
    feed = SchwabFeed(make_client(
        [("GET", "/pricehistory", FakeResponse(CANDLES))], tmp_path))
    bars = feed.get_bars("AAPL", 2)
    assert [b.close for b in bars] == [11.0, 13.0]
    assert bars[0].ts < bars[1].ts


def test_daily_interval_requests_daily_candles(tmp_path):
    feed = SchwabFeed(make_client(
        [("GET", "/pricehistory", FakeResponse(CANDLES))], tmp_path),
        interval="1d")
    feed.get_bars("AAPL", 2)
    params = feed.client._session.calls[0]["params"]
    assert params["frequencyType"] == "daily"


def test_hourly_interval_requests_thirty_minute_candles(tmp_path):
    """Schwab has no hourly candle, so 1h is built from 30m."""
    feed = SchwabFeed(make_client(
        [("GET", "/pricehistory", FakeResponse(CANDLES))], tmp_path),
        interval="1h")
    feed.get_bars("AAPL", 2)
    params = feed.client._session.calls[0]["params"]
    assert params["frequencyType"] == "minute" and params["frequency"] == 30


def _bar(hour, minute, close):
    return Bar(ts=datetime(2026, 8, 5, hour, minute, tzinfo=timezone.utc),
               open=close, high=close + 1, low=close - 1, close=close,
               volume=10.0)


def test_half_hour_bars_fold_into_hours():
    out = _pair_into_hours([_bar(14, 0, 10), _bar(14, 30, 12),
                            _bar(15, 0, 13), _bar(15, 30, 11)])
    assert len(out) == 2
    assert out[0].open == 10 and out[0].close == 12
    assert out[0].high == 13 and out[0].volume == 20.0


def test_hourly_fold_does_not_merge_across_a_session_gap():
    """A pairwise fold would glue Tuesday's close to Wednesday's open."""
    monday = Bar(ts=datetime(2026, 8, 4, 20, 30, tzinfo=timezone.utc),
                 open=1, high=1, low=1, close=1, volume=1)
    tuesday = Bar(ts=datetime(2026, 8, 5, 13, 30, tzinfo=timezone.utc),
                  open=2, high=2, low=2, close=2, volume=1)
    assert len(_pair_into_hours([monday, tuesday])) == 2


def test_empty_candles_serve_the_cache(tmp_path):
    feed = SchwabFeed(make_client(
        [("GET", "/pricehistory",
          [FakeResponse(CANDLES), FakeResponse({"empty": True})])], tmp_path))
    assert len(feed.get_bars("AAPL", 2)) == 2
    feed.cache_seconds = 0
    assert len(feed.get_bars("AAPL", 2)) == 2


# ---- fill accounting ------------------------------------------------------

def test_average_fill_price_is_quantity_weighted():
    """Partial executions must be averaged, not last-one-wins."""
    order = {"orderActivityCollection": [{"executionLegs": [
        {"quantity": 1, "price": 100.0},
        {"quantity": 3, "price": 104.0},
    ]}]}
    qty, price = average_fill_price(order)
    assert qty == pytest.approx(4.0)
    assert price == pytest.approx(103.0)


def test_average_fill_price_falls_back_to_summary_fields():
    qty, price = average_fill_price({"filledQuantity": 2, "price": 50.0})
    assert (qty, price) == (2.0, 50.0)


def test_average_fill_price_of_an_unfilled_order_is_zero():
    assert average_fill_price({"status": "REJECTED"}) == (0.0, 0.0)


# ---- order submission -----------------------------------------------------

FILLED = {"status": "FILLED", "orderActivityCollection": [
    {"executionLegs": [{"quantity": 3, "price": 100.0}]}]}


def _order_routes(quote=100.0, positions=None, final=None):
    account = {"securitiesAccount": {
        "type": "MARGIN", "roundTrips": 0, "isDayTrader": False,
        "currentBalances": {"liquidationValue": 30000.0},
        "positions": positions or [],
    }}
    # Order matters: the account URL is a prefix of the order URL, so the more
    # specific route has to be listed first.
    return [
        ("GET", "/marketdata/v1/quotes",
         FakeResponse({"AAPL": {"quote": {"lastPrice": quote}}})),
        ("GET", "/orders/999", FakeResponse(final or FILLED)),
        ("GET", "/trader/v1/accounts/HASH1", FakeResponse(account)),
        ("POST", "/orders", FakeResponse(
            None, status_code=201,
            headers={"Location": "https://api.schwabapi.com/orders/999"})),
    ]


def _position(symbol="AAPL", qty=10.0):
    return {"instrument": {"symbol": symbol, "assetType": "EQUITY"},
            "longQuantity": qty}


def test_notional_is_floored_to_whole_shares(tmp_path):
    """Schwab has no fractional orders: $350 at $100 buys 3 shares, not 3.5."""
    client = make_client(_order_routes(quote=100.0), tmp_path)
    SchwabBroker(client).submit(Order("AAPL", AssetClass.STOCK, Side.BUY, 350.0))
    leg = client._session.sent_bodies("/orders")[0]["orderLegCollection"][0]
    assert leg["quantity"] == 3
    assert leg["instruction"] == "BUY"


def test_order_smaller_than_one_share_is_skipped(tmp_path):
    client = make_client(_order_routes(quote=300.0), tmp_path)
    assert SchwabBroker(client).submit(
        Order("AAPL", AssetClass.STOCK, Side.BUY, 100.0)) is None
    assert client._session.sent_bodies("/orders") == []


def test_exact_share_count_is_not_lost_to_float_noise(tmp_path):
    """3 shares * $309.38 fed back in must floor to 3, never 2."""
    price = 309.38
    client = make_client(_order_routes(quote=price), tmp_path)
    SchwabBroker(client).submit(
        Order("AAPL", AssetClass.STOCK, Side.BUY, 3 * price))
    leg = client._session.sent_bodies("/orders")[0]["orderLegCollection"][0]
    assert leg["quantity"] == 3


def test_order_id_is_parsed_from_the_location_header(tmp_path):
    """Schwab returns 201 with an empty body; the id is only in the header."""
    client = make_client(_order_routes(), tmp_path)
    fill = SchwabBroker(client).submit(
        Order("AAPL", AssetClass.STOCK, Side.BUY, 350.0))
    assert fill is not None and fill.quantity == pytest.approx(3.0)
    assert fill.price == pytest.approx(100.0)


def test_sell_is_clamped_to_shares_actually_held(tmp_path):
    client = make_client(
        _order_routes(quote=100.0, positions=[_position(qty=2.0)]), tmp_path)
    SchwabBroker(client).submit(
        Order("AAPL", AssetClass.STOCK, Side.SELL, 1000.0))
    leg = client._session.sent_bodies("/orders")[0]["orderLegCollection"][0]
    assert leg["quantity"] == 2
    assert leg["instruction"] == "SELL"


def test_sell_without_a_position_sends_nothing(tmp_path):
    client = make_client(_order_routes(positions=[]), tmp_path)
    assert SchwabBroker(client).submit(
        Order("AAPL", AssetClass.STOCK, Side.SELL, 500.0)) is None
    assert client._session.sent_bodies("/orders") == []


def test_crypto_is_refused(tmp_path):
    client = make_client(_order_routes(), tmp_path)
    assert SchwabBroker(client).submit(
        Order("BTC-USD", AssetClass.CRYPTO, Side.BUY, 500.0)) is None


def test_rejected_order_books_nothing(tmp_path):
    client = make_client(
        _order_routes(final={"status": "REJECTED"}), tmp_path)
    assert SchwabBroker(client).submit(
        Order("AAPL", AssetClass.STOCK, Side.BUY, 350.0)) is None


def test_partial_fill_is_booked_for_what_filled(tmp_path):
    partial = {"status": "CANCELED", "orderActivityCollection": [
        {"executionLegs": [{"quantity": 1, "price": 100.0}]}]}
    client = make_client(_order_routes(final=partial), tmp_path)
    fill = SchwabBroker(client).submit(
        Order("AAPL", AssetClass.STOCK, Side.BUY, 350.0))
    assert fill.quantity == pytest.approx(1.0)


def test_working_order_is_cancelled_on_timeout(tmp_path):
    client = make_client([
        ("GET", "/orders/999", FakeResponse({"status": "WORKING"})),
        ("DELETE", "/orders/999", FakeResponse({})),
    ], tmp_path)
    client.await_fill("999", timeout_seconds=0.05, poll_seconds=0.0,
                      sleep=lambda _: None)
    assert any(c["method"] == "DELETE" for c in client._session.calls)


# ---- account introspection ------------------------------------------------

def test_account_snapshot_reads_schwab_round_trips(tmp_path):
    client = make_client(_order_routes(), tmp_path)
    snap = SchwabBroker(client).account_snapshot()
    assert snap["equity"] == pytest.approx(30000.0)
    assert snap["daytrade_count"] == 0


def test_held_quantities_are_reported_for_drift_checks(tmp_path):
    client = make_client(
        _order_routes(positions=[_position("AAPL", 5.0),
                                 _position("MSFT", 2.0)]), tmp_path)
    assert SchwabBroker(client).held_quantities() == {"AAPL": 5.0, "MSFT": 2.0}


def test_schwab_always_reports_itself_as_live(tmp_path):
    assert SchwabBroker(make_client([], tmp_path)).is_live() is True


# ---- config guard rails ---------------------------------------------------

def _cfg(**kw):
    cfg = Config(watchlist=[WatchItem("AAPL", AssetClass.STOCK, "sma_crossover")],
                 risk=RiskConfig(),
                 # A live config must not share the default book with a paper
                 # run; validation enforces that, so give it its own.
                 state_file="state/portfolio-schwab.json",
                 secrets=Secrets(schwab_app_key="k", schwab_app_secret="s"))
    for k, v in kw.items():
        setattr(cfg, k, v)
    return cfg


def test_schwab_in_paper_mode_is_refused():
    """The API has no paper account — this combination must never start."""
    with pytest.raises(ValueError, match="no paper account"):
        _validate(_cfg(broker="schwab", mode="paper"))


def test_schwab_live_is_accepted():
    _validate(_cfg(broker="schwab", mode="live"))


def test_schwab_without_keys_is_refused():
    with pytest.raises(ValueError, match="SCHWAB_APP_KEY"):
        _validate(_cfg(broker="schwab", mode="live", secrets=Secrets()))


def test_schwab_rejects_a_crypto_watchlist():
    cfg = _cfg(broker="schwab", mode="live")
    cfg.watchlist.append(WatchItem("BTC-USD", AssetClass.CRYPTO, "sma_crossover"))
    with pytest.raises(ValueError, match="cannot trade crypto"):
        _validate(cfg)


def test_auto_never_selects_schwab():
    """Live money must be an explicit choice, never inferred from .env."""
    assert resolve_broker(_cfg(broker="auto", mode="live")) == "robinhood"
