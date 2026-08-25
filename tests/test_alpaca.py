"""Tests for the Alpaca adapter — the paths where a bug moves real money.

Everything here runs against a fake HTTP session, so no network and no keys.
The focus is on the three things that cost money if they are wrong: what price
a fill is booked at, how much gets sold, and whether an unfilled order can be
left live at the broker.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from rhbot.alpaca_client import (AlpacaClient, from_alpaca_symbol,
                                 lookback_start, position_symbol,
                                 to_alpaca_symbol)
from rhbot.brokers.alpaca import AlpacaBroker
from rhbot.config import Config, RiskConfig, Secrets, WatchItem, _validate
from rhbot.data.alpaca_feed import AlpacaFeed
from rhbot.factory import resolve_broker, warn_on_position_drift
from rhbot.models import AssetClass, Fill, Order, Side
from rhbot.portfolio import Portfolio


# ---- fake transport -------------------------------------------------------

class FakeResponse:
    content = b"{}"
    text = ""

    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def json(self):
        return self._payload


class FakeSession:
    """Routes on (METHOD, url-substring). A list value is consumed in order."""

    def __init__(self, routes):
        self.routes = list(routes)
        self.calls = []
        self.headers = {}

    def request(self, method, url, params=None, json=None, timeout=None):
        self.calls.append({"method": method, "url": url,
                           "params": params, "json": json})
        for route in self.routes:
            m, frag, resp = route
            if m == method and frag in url:
                if isinstance(resp, list):
                    return resp.pop(0) if len(resp) > 1 else resp[0]
                return resp
        return FakeResponse({}, status_code=404)

    def sent_bodies(self, frag):
        return [c["json"] for c in self.calls
                if c["json"] and frag in c["url"]]


def make_client(routes, paper=True):
    client = AlpacaClient("key", "secret", paper=paper)
    client._session = FakeSession(routes)
    return client


ORDER_ID = "ord-1"


def order_payload(status="filled", filled_qty="4.0", avg="25.50"):
    return {"id": ORDER_ID, "status": status,
            "filled_qty": filled_qty, "filled_avg_price": avg}


# ---- symbols and hosts ----------------------------------------------------

def test_crypto_symbols_are_translated():
    assert to_alpaca_symbol("BTC-USD", is_crypto=True) == "BTC/USD"
    assert to_alpaca_symbol("AAPL", is_crypto=False) == "AAPL"


def test_paper_and_live_hit_different_hosts():
    assert "paper-api" in make_client([], paper=True).trading_url
    assert "paper-api" not in make_client([], paper=False).trading_url


def test_paper_broker_is_not_live():
    assert AlpacaBroker(make_client([], paper=True)).is_live() is False
    assert AlpacaBroker(make_client([], paper=False)).is_live() is True


# ---- prices ---------------------------------------------------------------

def test_crypto_price_is_the_midpoint():
    c = make_client([("GET", "latest/quotes",
                      FakeResponse({"quotes": {"BTC/USD": {"bp": "100.0",
                                                           "ap": "102.0"}}}))])
    assert c.get_price("BTC-USD", is_crypto=True) == pytest.approx(101.0)


def test_one_sided_book_does_not_halve_the_price():
    """A zero on the missing side would average out to half the real price."""
    c = make_client([("GET", "latest/quotes",
                      FakeResponse({"quotes": {"BTC/USD": {"bp": "100.0",
                                                           "ap": "0"}}}))])
    assert c.get_price("BTC-USD", is_crypto=True) == pytest.approx(100.0)


def test_stock_price_uses_last_trade():
    c = make_client([("GET", "trades/latest",
                      FakeResponse({"trades": {"AAPL": {"p": 212.34}}}))])
    assert c.get_price("AAPL", is_crypto=False) == pytest.approx(212.34)


def test_share_class_symbols_are_not_mistaken_for_crypto():
    """`BRK-B` has a dash but is a stock — routing it to crypto would 404."""
    broker = AlpacaBroker(make_client([]))
    assert broker._is_crypto("BRK-B") is False
    assert broker._is_crypto("BTC-USD") is True
    assert broker._is_crypto("AAPL") is False


def test_watchlist_overrides_the_symbol_heuristic():
    broker = AlpacaBroker(make_client([]), {"WEIRD-USD": AssetClass.STOCK})
    assert broker._is_crypto("WEIRD-USD") is False


def test_missing_quote_returns_none_not_zero():
    c = make_client([("GET", "trades/latest", FakeResponse({"trades": {}}))])
    assert c.get_price("AAPL", is_crypto=False) is None


def test_http_error_returns_none():
    c = make_client([("GET", "trades/latest", FakeResponse({}, status_code=403))])
    assert c.get_price("AAPL", is_crypto=False) is None


# ---- bars -----------------------------------------------------------------

# Queried with sort=desc, so the API answers NEWEST FIRST. Fixtures mirror that;
# the client is responsible for flipping it back to chronological order.
BARS = {"bars": {"AAPL": [
    {"t": "2026-08-04T20:00:00Z", "o": 11, "h": 14, "l": 11, "c": 13, "v": 150},
    {"t": "2026-08-03T20:00:00Z", "o": 10, "h": 12, "l": 9, "c": 11, "v": 100},
]}}


def _feed(routes, interval="1d"):
    return AlpacaFeed(make_client(routes), {"AAPL": AssetClass.STOCK,
                                            "BTC-USD": AssetClass.CRYPTO},
                      interval=interval)


def test_bars_are_parsed_oldest_first():
    """The newest-first API response must come back chronological."""
    bars = _feed([("GET", "/v2/stocks/bars", FakeResponse(BARS))]).get_bars("AAPL", 2)
    assert [b.close for b in bars] == [11.0, 13.0]
    assert bars[0].ts < bars[1].ts
    assert bars[1].high == pytest.approx(14.0)


def test_malformed_bars_are_skipped_not_fatal():
    payload = {"bars": {"AAPL": [{"t": "nonsense"}, BARS["bars"]["AAPL"][0]]}}
    bars = _feed([("GET", "/v2/stocks/bars", FakeResponse(payload))]).get_bars("AAPL", 5)
    assert [b.close for b in bars] == [13.0]


def test_crypto_bars_use_the_crypto_endpoint():
    payload = {"bars": {"BTC/USD": BARS["bars"]["AAPL"]}}
    feed = _feed([("GET", "/v1beta3/crypto", FakeResponse(payload))])
    assert len(feed.get_bars("BTC-USD", 2)) == 2


def test_bars_request_sends_a_start_date():
    """Without `start` Alpaca returns ONLY the latest bar, starving warmup."""
    feed = _feed([("GET", "/v2/stocks/bars", FakeResponse(BARS))])
    feed.get_bars("AAPL", 100)
    assert feed.client._session.calls[0]["params"]["start"]


def test_bars_request_sorts_newest_first():
    """Ascending + limit silently truncates the NEWEST bars, not the oldest."""
    feed = _feed([("GET", "/v2/stocks/bars", FakeResponse(BARS))])
    feed.get_bars("AAPL", 100)
    assert feed.client._session.calls[0]["params"]["sort"] == "desc"


def test_lookback_spans_more_calendar_days_than_sessions():
    """100 daily bars need >100 calendar days — weekends aren't trading days."""
    now = datetime(2026, 8, 10, tzinfo=timezone.utc)
    start = lookback_start("1Day", 100, is_crypto=False, now=now)
    days = (now - datetime.strptime(start, "%Y-%m-%dT%H:%M:%SZ")
            .replace(tzinfo=timezone.utc)).days
    assert days > 100


def test_crypto_lookback_is_not_padded_for_weekends():
    """Crypto trades 24/7, so 100 daily bars really is ~100 days."""
    now = datetime(2026, 8, 10, tzinfo=timezone.utc)
    start = lookback_start("1Day", 100, is_crypto=True, now=now)
    days = (now - datetime.strptime(start, "%Y-%m-%dT%H:%M:%SZ")
            .replace(tzinfo=timezone.utc)).days
    assert 100 <= days <= 105


def test_intraday_lookback_accounts_for_the_short_session():
    """390 one-minute bars is more than one calendar day of market hours."""
    now = datetime(2026, 8, 10, tzinfo=timezone.utc)
    start = lookback_start("1Min", 390, is_crypto=False, now=now)
    days = (now - datetime.strptime(start, "%Y-%m-%dT%H:%M:%SZ")
            .replace(tzinfo=timezone.utc)).days
    assert days >= 6


def test_interval_maps_to_alpaca_timeframe():
    feed = _feed([("GET", "/v2/stocks/bars", FakeResponse(BARS))], interval="5m")
    feed.get_bars("AAPL", 2)
    assert feed.client._session.calls[0]["params"]["timeframe"] == "5Min"


def test_empty_response_serves_cached_bars():
    """An outage must not look like 'no data' and flush good history."""
    feed = _feed([("GET", "/v2/stocks/bars",
                   [FakeResponse(BARS), FakeResponse({"bars": {}})])])
    assert len(feed.get_bars("AAPL", 2)) == 2
    feed.cache_seconds = 0  # force a refetch, which now fails
    assert len(feed.get_bars("AAPL", 2)) == 2


# ---- order submission -----------------------------------------------------

def _buy_routes(order=None):
    return [
        ("POST", "/v2/orders", FakeResponse(order or order_payload())),
        ("GET", "/v2/orders/", FakeResponse(order or order_payload())),
    ]


def test_buy_sends_notional_not_quantity():
    client = make_client(_buy_routes())
    broker = AlpacaBroker(client)
    broker.submit(Order("AAPL", AssetClass.STOCK, Side.BUY, 102.0))
    body = client._session.sent_bodies("/v2/orders")[0]
    assert body["notional"] == "102.0"
    assert "qty" not in body
    assert body["side"] == "buy" and body["type"] == "market"


def test_fill_is_booked_at_the_real_fill_price():
    """The whole point of polling: book 25.50, not the pre-trade quote."""
    client = make_client(_buy_routes(order_payload(filled_qty="4", avg="25.50")))
    fill = AlpacaBroker(client).submit(
        Order("AAPL", AssetClass.STOCK, Side.BUY, 100.0))
    assert fill.quantity == pytest.approx(4.0)
    assert fill.price == pytest.approx(25.50)
    assert fill.notional == pytest.approx(102.0)


def test_rejected_order_returns_none():
    client = make_client([("POST", "/v2/orders",
                           FakeResponse({}, status_code=422))])
    assert AlpacaBroker(client).submit(
        Order("AAPL", AssetClass.STOCK, Side.BUY, 100.0)) is None


def test_unfilled_order_books_nothing():
    client = make_client(_buy_routes(
        order_payload(status="canceled", filled_qty="0", avg=None)))
    assert AlpacaBroker(client).submit(
        Order("AAPL", AssetClass.STOCK, Side.BUY, 100.0)) is None


def test_partial_fill_is_booked_for_the_filled_amount():
    client = make_client(_buy_routes(
        order_payload(status="canceled", filled_qty="1.5", avg="10.0")))
    fill = AlpacaBroker(client).submit(
        Order("AAPL", AssetClass.STOCK, Side.BUY, 100.0))
    assert fill.quantity == pytest.approx(1.5)


def test_crypto_orders_are_gtc():
    """A `day` order would expire at the equities close; crypto trades 24/7."""
    client = make_client(_buy_routes())
    AlpacaBroker(client).submit(
        Order("BTC-USD", AssetClass.CRYPTO, Side.BUY, 500.0))
    body = client._session.sent_bodies("/v2/orders")[0]
    assert body["time_in_force"] == "gtc"
    assert body["symbol"] == "BTC/USD"


# ---- sells ----------------------------------------------------------------

def _sell_routes(available="10", price=20.0):
    return [
        ("GET", "trades/latest", FakeResponse({"trades": {"AAPL": {"p": price}}})),
        ("GET", "/v2/positions/AAPL",
         FakeResponse({"symbol": "AAPL", "qty": available,
                       "qty_available": available})),
        ("POST", "/v2/orders", FakeResponse(order_payload())),
        ("GET", "/v2/orders/", FakeResponse(order_payload())),
    ]


def test_sell_sends_quantity():
    client = make_client(_sell_routes())
    AlpacaBroker(client).submit(
        Order("AAPL", AssetClass.STOCK, Side.SELL, 100.0))
    body = client._session.sent_bodies("/v2/orders")[0]
    assert float(body["qty"]) == pytest.approx(5.0)  # $100 / $20
    assert "notional" not in body


def test_sell_is_clamped_to_the_quantity_actually_held():
    """Price drift between signal and order must not oversell into a reject."""
    client = make_client(_sell_routes(available="3"))
    AlpacaBroker(client).submit(
        Order("AAPL", AssetClass.STOCK, Side.SELL, 100.0))
    body = client._session.sent_bodies("/v2/orders")[0]
    assert float(body["qty"]) == pytest.approx(3.0)


def test_sell_with_no_position_sends_no_order():
    client = make_client(_sell_routes(available="0"))
    assert AlpacaBroker(client).submit(
        Order("AAPL", AssetClass.STOCK, Side.SELL, 100.0)) is None
    assert client._session.sent_bodies("/v2/orders") == []


def test_sell_without_a_price_sends_no_order():
    client = make_client([("GET", "trades/latest",
                           FakeResponse({"trades": {}}))])
    assert AlpacaBroker(client).submit(
        Order("AAPL", AssetClass.STOCK, Side.SELL, 100.0)) is None


# ---- await_fill -----------------------------------------------------------

def test_await_fill_returns_on_terminal_status():
    client = make_client([("GET", "/v2/orders/", FakeResponse(order_payload()))])
    assert client.await_fill(ORDER_ID, sleep=lambda _: None)["status"] == "filled"


def test_timeout_cancels_the_order():
    """Abandoning a live order is how the local book silently drifts."""
    pending = order_payload(status="new", filled_qty="0", avg=None)
    client = make_client([
        ("GET", "/v2/orders/", FakeResponse(pending)),
        ("DELETE", "/v2/orders/", FakeResponse({})),
    ])
    client.await_fill(ORDER_ID, timeout_seconds=0.05, poll_seconds=0.0,
                      sleep=lambda _: None)
    assert any(c["method"] == "DELETE" for c in client._session.calls)


# ---- live configs must not share the paper book ---------------------------

def test_live_config_must_declare_its_own_state_file():
    """Sharing state/portfolio.json would boot live holding the paper book."""
    cfg = Config(watchlist=[WatchItem("AAPL", AssetClass.STOCK, "sma_crossover")],
                 risk=RiskConfig(), mode="live", broker="alpaca",
                 secrets=Secrets(alpaca_live_api_key="k",
                                 alpaca_live_api_secret="s"))
    with pytest.raises(ValueError, match="own `state_file`"):
        _validate(cfg)


def test_live_config_with_its_own_state_file_is_accepted():
    cfg = Config(watchlist=[WatchItem("AAPL", AssetClass.STOCK, "sma_crossover")],
                 risk=RiskConfig(), mode="live", broker="alpaca",
                 state_file="state/portfolio-live.json",
                 secrets=Secrets(alpaca_live_api_key="k",
                                 alpaca_live_api_secret="s"))
    _validate(cfg)


def test_paper_keys_are_never_used_against_the_live_host():
    cfg = Config(watchlist=[WatchItem("AAPL", AssetClass.STOCK, "sma_crossover")],
                 risk=RiskConfig(), mode="live", broker="alpaca",
                 state_file="state/portfolio-live.json",
                 secrets=Secrets(alpaca_api_key="paper",
                                 alpaca_api_secret="paper"))
    with pytest.raises(ValueError, match="ALPACA_LIVE_API_KEY"):
        _validate(cfg)


# ---- factory wiring -------------------------------------------------------

def _cfg(**kw):
    cfg = Config(watchlist=[WatchItem("AAPL", AssetClass.STOCK, "sma_crossover")],
                 risk=RiskConfig())
    for k, v in kw.items():
        setattr(cfg, k, v)
    return cfg


def test_auto_prefers_alpaca_when_keys_exist():
    cfg = _cfg(secrets=Secrets(alpaca_api_key="k", alpaca_api_secret="s"))
    assert resolve_broker(cfg) == "alpaca"


def test_auto_falls_back_to_paper_without_keys():
    assert resolve_broker(_cfg()) == "paper"


def test_auto_uses_robinhood_for_live_without_alpaca_keys():
    assert resolve_broker(_cfg(mode="live")) == "robinhood"


def test_explicit_choice_wins_over_auto():
    cfg = _cfg(broker="paper",
               secrets=Secrets(alpaca_api_key="k", alpaca_api_secret="s"))
    assert resolve_broker(cfg) == "paper"


def test_alpaca_without_keys_is_rejected_at_load():
    with pytest.raises(ValueError, match="ALPACA_API_KEY"):
        _validate(_cfg(broker="alpaca"))


def test_unknown_broker_is_rejected():
    with pytest.raises(ValueError, match="broker must be"):
        _validate(_cfg(broker="etrade"))


# ---- position drift -------------------------------------------------------

class StubBroker:
    name = "stub"

    def __init__(self, held):
        self._held = held

    def held_quantities(self):
        return self._held


def test_drift_is_detected(tmp_path):
    p = Portfolio(1000.0, str(tmp_path / "s.json"))
    p.apply_fill(Fill("AAPL", AssetClass.STOCK, Side.BUY, 5, 10.0))
    assert warn_on_position_drift(StubBroker({"AAPL": 2.0}), p) is True


def test_matching_books_are_clean(tmp_path):
    p = Portfolio(1000.0, str(tmp_path / "s.json"))
    p.apply_fill(Fill("AAPL", AssetClass.STOCK, Side.BUY, 5, 10.0))
    assert warn_on_position_drift(StubBroker({"AAPL": 5.0}), p) is False


def test_position_held_only_at_the_broker_is_drift(tmp_path):
    p = Portfolio(1000.0, str(tmp_path / "s.json"))
    assert warn_on_position_drift(StubBroker({"TSLA": 1.0}), p) is True


def test_brokers_without_position_reporting_are_skipped(tmp_path):
    p = Portfolio(1000.0, str(tmp_path / "s.json"))
    assert warn_on_position_drift(object(), p) is False


# ---- market hours ---------------------------------------------------------

def _clock_routes(is_open):
    return [("GET", "/v2/clock", FakeResponse({"is_open": is_open}))]


def test_equity_order_is_refused_when_the_session_is_closed():
    """A `day` order placed while closed sits unfilled, gets cancelled, and the
    engine re-signals — 93 orders churned at the broker before this guard."""
    client = make_client(_clock_routes(False) + _buy_routes())
    assert AlpacaBroker(client).submit(
        Order("AAPL", AssetClass.STOCK, Side.BUY, 100.0)) is None
    assert client._session.sent_bodies("/v2/orders") == []


def test_crypto_still_trades_when_equities_are_closed():
    client = make_client(_clock_routes(False) + _buy_routes())
    fill = AlpacaBroker(client).submit(
        Order("BTC-USD", AssetClass.CRYPTO, Side.BUY, 500.0))
    assert fill is not None


def test_equity_order_goes_through_when_open():
    client = make_client(_clock_routes(True) + _buy_routes())
    assert AlpacaBroker(client).submit(
        Order("AAPL", AssetClass.STOCK, Side.BUY, 100.0)) is not None


def test_unreachable_clock_does_not_block_trading():
    """None means 'unknown'. A network blip must not silently halt the bot."""
    client = make_client([("GET", "/v2/clock", FakeResponse({}, status_code=503))]
                         + _buy_routes())
    assert AlpacaBroker(client).submit(
        Order("AAPL", AssetClass.STOCK, Side.BUY, 100.0)) is not None


def test_clock_is_cached_between_orders():
    client = make_client(_clock_routes(True) + _buy_routes())
    broker = AlpacaBroker(client)
    broker.submit(Order("AAPL", AssetClass.STOCK, Side.BUY, 100.0))
    broker.submit(Order("AAPL", AssetClass.STOCK, Side.BUY, 100.0))
    clock_calls = [c for c in client._session.calls if "/v2/clock" in c["url"]]
    assert len(clock_calls) == 1


# ---- crypto symbol spellings ----------------------------------------------

def test_alpaca_spells_one_crypto_asset_three_ways():
    """data/orders want BTC/USD, positions want BTCUSD, rhbot uses BTC-USD."""
    assert to_alpaca_symbol("BTC-USD", True) == "BTC/USD"
    assert position_symbol("BTC-USD", True) == "BTCUSD"
    assert from_alpaca_symbol("BTCUSD", True) == "BTC-USD"
    assert from_alpaca_symbol("BTC/USD", True) == "BTC-USD"


def test_longest_quote_currency_wins():
    """ETHUSDT must split as ETH/USDT, not ETH/USD with a stray T."""
    assert from_alpaca_symbol("ETHUSDT", True) == "ETH-USDT"
    assert from_alpaca_symbol("ETHUSDC", True) == "ETH-USDC"


def test_equities_are_never_rewritten():
    for fn in (to_alpaca_symbol, position_symbol, from_alpaca_symbol):
        assert fn("AAPL", False) == "AAPL"
        assert fn("BRK-B", False) == "BRK-B"


def test_position_lookup_uses_the_concatenated_form():
    """GET /v2/positions/BTC/USD 404s — the slash reads as a path separator."""
    client = make_client([("GET", "/v2/positions/BTCUSD",
                           FakeResponse({"symbol": "BTCUSD", "qty": "0.5",
                                         "qty_available": "0.5"}))])
    assert client.get_position("BTC-USD", is_crypto=True)["qty"] == "0.5"


def test_held_quantities_normalises_crypto_back_to_rhbot_form():
    """Otherwise every crypto holding false-alarms the drift check forever."""
    client = make_client([("GET", "/v2/positions", FakeResponse([
        {"symbol": "BTCUSD", "qty": "0.25", "asset_class": "crypto"},
        {"symbol": "AAPL", "qty": "10", "asset_class": "us_equity"},
    ]))])
    assert AlpacaBroker(client).held_quantities() == {"BTC-USD": 0.25,
                                                      "AAPL": 10.0}


def test_crypto_sell_is_clamped_to_the_real_position():
    """The clamp silently no-op'd while the position lookup was 404ing."""
    client = make_client([
        ("GET", "latest/quotes",
         FakeResponse({"quotes": {"BTC/USD": {"bp": "100.0", "ap": "100.0"}}})),
        ("GET", "/v2/positions/BTCUSD",
         FakeResponse({"symbol": "BTCUSD", "qty": "0.5",
                       "qty_available": "0.5"})),
        ("POST", "/v2/orders", FakeResponse(order_payload())),
        ("GET", "/v2/orders/", FakeResponse(order_payload())),
    ])
    AlpacaBroker(client).submit(
        Order("BTC-USD", AssetClass.CRYPTO, Side.SELL, 1000.0))
    body = client._session.sent_bodies("/v2/orders")[0]
    assert float(body["qty"]) == pytest.approx(0.5)  # not 1000/100 = 10


# ---- a fresh live deploy must adopt the broker, not invent a balance ------

def _live_cfg(tmp_path):
    return Config(
        watchlist=[WatchItem("AAPL", AssetClass.STOCK, "sma_crossover")],
        risk=RiskConfig(), mode="live", broker="alpaca",
        paper_starting_cash=10_000.0,
        state_file=str(tmp_path / "live.json"),
        secrets=Secrets(alpaca_live_api_key="k", alpaca_live_api_secret="s"))


class _StubBroker:
    name = "alpaca-live"

    def __init__(self, cash, positions):
        self._cash, self._pos = cash, positions

    def account_snapshot(self):
        return {"cash": self._cash, "equity": self._cash, "daytrade_count": 0,
                "pattern_day_trader": False, "blocked": False, "status": "ACTIVE"}

    def position_details(self):
        return self._pos


def test_fresh_live_book_is_seeded_from_the_broker(tmp_path):
    """Otherwise it invents $10,000 and the cash guard bricks the deploy."""
    import json as _json
    from rhbot.factory import seed_live_book_from_broker
    cfg = _live_cfg(tmp_path)
    broker = _StubBroker(240.0, {
        "SMCI": {"quantity": 1.5, "avg_price": 40.0,
                 "asset_class": AssetClass.STOCK}})

    assert seed_live_book_from_broker(cfg, broker, cfg.state_file) is True
    book = _json.load(open(cfg.state_file))
    assert book["cash"] == pytest.approx(240.0)
    assert book["positions"]["SMCI"]["quantity"] == pytest.approx(1.5)
    assert book["positions"]["SMCI"]["avg_price"] == pytest.approx(40.0)
    # equity = cash + basis, not paper_starting_cash
    assert book["starting_cash"] == pytest.approx(300.0)


def test_existing_live_book_is_never_overwritten(tmp_path):
    import json as _json
    from rhbot.factory import seed_live_book_from_broker
    cfg = _live_cfg(tmp_path)
    with open(cfg.state_file, "w") as f:
        _json.dump({"cash": 99.0, "positions": {}}, f)

    assert seed_live_book_from_broker(cfg, _StubBroker(240.0, {}),
                                      cfg.state_file) is False
    assert _json.load(open(cfg.state_file))["cash"] == 99.0


def test_paper_configs_are_left_alone(tmp_path):
    from rhbot.factory import seed_live_book_from_broker
    cfg = _live_cfg(tmp_path)
    cfg.mode = "paper"
    assert seed_live_book_from_broker(cfg, _StubBroker(240.0, {}),
                                      cfg.state_file) is False


def test_seeded_book_passes_the_cash_guard(tmp_path):
    """Seed then verify — the two must agree or the deploy still bricks."""
    from rhbot.factory import seed_live_book_from_broker, verify_cash_matches_broker
    from rhbot.portfolio import Portfolio
    cfg = _live_cfg(tmp_path)
    broker = _StubBroker(240.0, {})
    seed_live_book_from_broker(cfg, broker, cfg.state_file)
    p = Portfolio.load_or_new(cfg.paper_starting_cash, cfg.state_file)
    verify_cash_matches_broker(broker, p, cfg)   # must not raise


# ---- per-symbol bar intervals ---------------------------------------------

def test_feed_uses_the_per_symbol_interval():
    """Crypto on 15m and equities on 1d must coexist in ONE engine.

    Two engines on one brokerage account each seed a book from the same cash
    and both believe they own all of it.
    """
    feed = AlpacaFeed(make_client([("GET", "/v2/stocks/bars",
                                    FakeResponse(BARS))]),
                      {"AAPL": AssetClass.STOCK, "BTC-USD": AssetClass.CRYPTO},
                      interval="1d", symbol_interval={"BTC-USD": "15m"})
    assert feed.timeframe_for("AAPL") == "1Day"
    assert feed.timeframe_for("BTC-USD") == "15Min"


def test_per_symbol_interval_reaches_the_request():
    payload = {"bars": {"BTC/USD": BARS["bars"]["AAPL"]}}
    feed = AlpacaFeed(make_client([("GET", "/v1beta3/crypto",
                                    FakeResponse(payload))]),
                      {"BTC-USD": AssetClass.CRYPTO},
                      interval="1d", symbol_interval={"BTC-USD": "15m"})
    feed.get_bars("BTC-USD", 2)
    assert feed.client._session.calls[0]["params"]["timeframe"] == "15Min"


def test_staleness_threshold_follows_the_symbols_interval():
    """A 15m symbol judged by the 1d limit would trade on last week's bars."""
    cfg = Config(watchlist=[
        WatchItem("AAPL", AssetClass.STOCK, "sma_crossover"),
        WatchItem("BTC-USD", AssetClass.CRYPTO, "sma_crossover",
                  bar_interval="15m")], bar_interval="1d")
    assert cfg.bar_age_limit_for("AAPL") == 5760      # 4 days
    assert cfg.bar_age_limit_for("BTC-USD") == 120    # 2 hours


def test_intraday_equity_is_refused_while_the_pdt_guard_is_on():
    """Intraday equity round trips ARE day trades: 3 per 5 days under $25k."""
    cfg = Config(
        watchlist=[WatchItem("AAPL", AssetClass.STOCK, "sma_crossover",
                             bar_interval="15m")],
        risk=RiskConfig(pdt_guard=True))
    with pytest.raises(ValueError, match="day trades"):
        _validate(cfg)


def test_intraday_crypto_is_allowed():
    cfg = Config(
        watchlist=[WatchItem("BTC-USD", AssetClass.CRYPTO, "sma_crossover",
                             bar_interval="15m")],
        risk=RiskConfig(pdt_guard=True))
    _validate(cfg)


def test_unknown_per_symbol_interval_is_rejected():
    cfg = Config(watchlist=[WatchItem("BTC-USD", AssetClass.CRYPTO,
                                      "sma_crossover", bar_interval="7m")])
    with pytest.raises(ValueError, match="bar_interval must be one of"):
        _validate(cfg)
