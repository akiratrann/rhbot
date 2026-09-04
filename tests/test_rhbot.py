"""Tests for the pieces where a bug costs real money: indicators, risk, portfolio."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from rhbot.backtest import run_backtest
from rhbot.brokers.paper import PaperBroker
from rhbot.config import RiskConfig
from rhbot.data.feed import SyntheticFeed
from rhbot.indicators import crossed_above, crossed_below, ema, rsi, sma
from rhbot.models import AssetClass, Bar, Fill, Order, Side, SignalType
from rhbot.portfolio import Portfolio
from rhbot.risk import RiskManager
from rhbot.strategy.slope_reversal import SlopeReversal
from rhbot.strategy.sma_crossover import SmaCrossover


def mkbars(closes):
    now = datetime.now(timezone.utc)
    return [Bar(ts=now - timedelta(minutes=len(closes) - i), open=c, high=c,
                low=c, close=c, volume=1.0) for i, c in enumerate(closes)]


# ---- indicators -----------------------------------------------------------

def test_sma_basic():
    assert sma([1, 2, 3, 4], 2) == [None, 1.5, 2.5, 3.5]


def test_sma_insufficient_data():
    assert sma([1, 2], 5) == [None, None]


def test_ema_seeds_with_sma():
    out = ema([1, 2, 3, 4, 5], 3)
    assert out[0] is None and out[1] is None
    assert out[2] == pytest.approx(2.0)  # SMA of 1,2,3


def test_rsi_all_gains_is_100():
    out = rsi([float(i) for i in range(1, 20)], 14)
    assert out[-1] == pytest.approx(100.0)


def test_rsi_range():
    vals = [10, 11, 10.5, 12, 11.5, 13, 12.5, 14, 13.5, 15,
            14.5, 16, 15.5, 17, 16.5, 18]
    for v in rsi(vals, 14):
        if v is not None:
            assert 0.0 <= v <= 100.0


def test_crossings():
    fast = [1.0, 3.0]
    slow = [2.0, 2.0]
    assert crossed_above(fast, slow)
    assert not crossed_below(fast, slow)
    assert crossed_below([3.0, 1.0], [2.0, 2.0])


def test_crossing_ignores_none():
    assert not crossed_above([None, 3.0], [2.0, 2.0])


# ---- portfolio ------------------------------------------------------------

def test_buy_then_sell_realizes_pnl(tmp_path):
    p = Portfolio(1000.0, str(tmp_path / "s.json"))
    p.apply_fill(Fill("X", AssetClass.STOCK, Side.BUY, 10, 10.0))
    assert p.cash == pytest.approx(900.0)
    assert p.positions["X"].avg_price == pytest.approx(10.0)

    p.apply_fill(Fill("X", AssetClass.STOCK, Side.SELL, 10, 12.0))
    assert p.realized_pnl == pytest.approx(20.0)
    assert p.cash == pytest.approx(1020.0)
    assert p.open_positions() == []


def test_average_cost_basis(tmp_path):
    p = Portfolio(1000.0, str(tmp_path / "s.json"))
    p.apply_fill(Fill("X", AssetClass.STOCK, Side.BUY, 10, 10.0))
    p.apply_fill(Fill("X", AssetClass.STOCK, Side.BUY, 10, 20.0))
    assert p.positions["X"].avg_price == pytest.approx(15.0)


def test_persistence_roundtrip(tmp_path):
    path = str(tmp_path / "s.json")
    p = Portfolio(1000.0, path)
    p.apply_fill(Fill("X", AssetClass.STOCK, Side.BUY, 5, 10.0))
    p.save()

    q = Portfolio.load_or_new(1000.0, path)
    assert q.cash == pytest.approx(p.cash)
    assert q.positions["X"].quantity == pytest.approx(5.0)


# ---- risk -----------------------------------------------------------------

def _risk(**kw):
    cfg = RiskConfig(max_order_notional=1000, max_total_exposure=2000,
                     max_open_positions=2, max_daily_loss=-100,
                     kill_switch_file="/nonexistent/STOP")
    for k, v in kw.items():
        setattr(cfg, k, v)
    return RiskManager(cfg)


def test_risk_blocks_oversized_order(tmp_path):
    r = _risk()
    p = Portfolio(10_000.0, str(tmp_path / "s.json"))
    o = Order("X", AssetClass.STOCK, Side.BUY, 5000.0)
    assert "max_order_notional" in r.check(o, p, {})


def test_risk_blocks_insufficient_cash(tmp_path):
    r = _risk()
    p = Portfolio(100.0, str(tmp_path / "s.json"))
    o = Order("X", AssetClass.STOCK, Side.BUY, 500.0)
    assert "insufficient cash" in r.check(o, p, {})


def test_risk_blocks_too_many_positions(tmp_path):
    r = _risk()
    p = Portfolio(10_000.0, str(tmp_path / "s.json"))
    p.apply_fill(Fill("A", AssetClass.STOCK, Side.BUY, 1, 10.0))
    p.apply_fill(Fill("B", AssetClass.STOCK, Side.BUY, 1, 10.0))
    o = Order("C", AssetClass.STOCK, Side.BUY, 100.0)
    assert "max_open_positions" in r.check(o, p, {})


def test_risk_blocks_exposure_cap(tmp_path):
    r = _risk()
    p = Portfolio(10_000.0, str(tmp_path / "s.json"))
    p.apply_fill(Fill("A", AssetClass.STOCK, Side.BUY, 100, 18.0))  # $1800
    o = Order("A", AssetClass.STOCK, Side.BUY, 500.0)
    assert "exposure" in r.check(o, p, {"A": 18.0})


def test_risk_allows_sell_of_held_position(tmp_path):
    r = _risk()
    p = Portfolio(10_000.0, str(tmp_path / "s.json"))
    p.apply_fill(Fill("A", AssetClass.STOCK, Side.BUY, 10, 10.0))
    o = Order("A", AssetClass.STOCK, Side.SELL, 100.0)
    assert r.check(o, p, {"A": 10.0}) is None


def test_risk_blocks_sell_without_position(tmp_path):
    r = _risk()
    p = Portfolio(10_000.0, str(tmp_path / "s.json"))
    o = Order("A", AssetClass.STOCK, Side.SELL, 100.0)
    assert "no position" in r.check(o, p, {})


def test_kill_switch_halts(tmp_path):
    switch = tmp_path / "STOP"
    switch.write_text("halt")
    r = _risk(kill_switch_file=str(switch))
    p = Portfolio(10_000.0, str(tmp_path / "s.json"))
    assert r.check_global_halts(p, {}) is True
    assert r.halted()
    # Once halted, every order is rejected.
    o = Order("A", AssetClass.STOCK, Side.BUY, 10.0)
    assert "halted" in r.check(o, p, {})


def test_daily_loss_halts(tmp_path):
    """Halts trading, but deliberately does NOT abort the tick.

    Returning False here is the point: the engine keeps processing symbols so
    exits can still fire. Only the kill switch stops everything.
    """
    r = _risk(max_daily_loss=-100)
    p = Portfolio(1000.0, str(tmp_path / "s.json"))
    p.apply_fill(Fill("X", AssetClass.STOCK, Side.BUY, 10, 100.0))

    assert r.check_global_halts(p, {"X": 50.0}) is False
    assert r.halted() is True
    assert r.blocks_exits() is False
    assert "daily loss" in r.halt_reason()


def test_sma_crossover_rejects_bad_periods():
    with pytest.raises(ValueError):
        SmaCrossover({"fast": 30, "slow": 10})


def test_sma_crossover_holds_during_warmup():
    s = SmaCrossover({"fast": 2, "slow": 4})
    assert s.evaluate("X", mkbars([1, 2]), None).type == SignalType.HOLD


def test_slope_reversal_buys_at_local_bottom():
    s = SlopeReversal({"smooth": 1})
    # Falling then rising => slope flips - to + => ENTER_LONG when flat.
    bars = mkbars([10, 9, 8, 7, 8])
    assert s.evaluate("X", bars, None).type == SignalType.ENTER_LONG


def test_slope_reversal_no_buy_when_already_long():
    from rhbot.models import Position
    s = SlopeReversal({"smooth": 1})
    pos = Position("X", AssetClass.STOCK, quantity=5, avg_price=9.0)
    bars = mkbars([10, 9, 8, 7, 8])
    assert s.evaluate("X", bars, pos).type == SignalType.HOLD


def test_slope_reversal_deadband_suppresses_noise():
    s = SlopeReversal({"smooth": 1, "min_slope_pct": 0.10})  # 10% deadband
    bars = mkbars([10, 9, 8, 7, 7.01])  # tiny uptick
    assert s.evaluate("X", bars, None).type == SignalType.HOLD


# ---- paper broker & backtest ---------------------------------------------

def test_paper_broker_applies_slippage():
    feed = SyntheticFeed(base_prices={"X": 100.0})
    b = PaperBroker(feed, slippage_bps=100.0)  # 1%
    price = feed.get_price("X")
    fill = b.submit(Order("X", AssetClass.STOCK, Side.BUY, 1000.0))
    assert fill is not None
    assert fill.price > price  # buys fill worse (higher)


def test_backtest_runs_and_counts_trades():
    closes = [10, 9, 8, 9, 10, 11, 10, 9, 10, 11, 12, 11, 10, 11, 12]
    r = run_backtest("t", SlopeReversal({"smooth": 1}), mkbars(closes),
                     starting_cash=1000.0, order_notional=1000.0,
                     bar_minutes=1.0)
    assert r.trades > 0
    assert r.end_equity > 0


# ---- day-trade tracking & PDT guard --------------------------------------

def test_same_day_roundtrip_counts_as_day_trade(tmp_path):
    p = Portfolio(10_000.0, str(tmp_path / "s.json"))
    p.apply_fill(Fill("A", AssetClass.STOCK, Side.BUY, 10, 10.0))
    p.apply_fill(Fill("A", AssetClass.STOCK, Side.SELL, 10, 11.0))
    assert p.day_trades_in_window() == 1


def test_overnight_hold_is_not_a_day_trade(tmp_path):
    p = Portfolio(10_000.0, str(tmp_path / "s.json"))
    p.apply_fill(Fill("A", AssetClass.STOCK, Side.BUY, 10, 10.0))
    p.positions["A"].last_buy_date = "1999-01-04"  # bought long ago
    p.apply_fill(Fill("A", AssetClass.STOCK, Side.SELL, 10, 11.0))
    assert p.day_trades_in_window() == 0


def test_crypto_roundtrip_is_never_a_day_trade(tmp_path):
    p = Portfolio(10_000.0, str(tmp_path / "s.json"))
    p.apply_fill(Fill("BTC-USD", AssetClass.CRYPTO, Side.BUY, 1, 100.0))
    p.apply_fill(Fill("BTC-USD", AssetClass.CRYPTO, Side.SELL, 1, 110.0))
    assert p.day_trades_in_window() == 0


def test_old_day_trades_fall_out_of_window(tmp_path):
    p = Portfolio(10_000.0, str(tmp_path / "s.json"))
    p.day_trade_dates = ["1999-01-04", "1999-01-05"]
    assert p.day_trades_in_window() == 0


def test_pdt_guard_blocks_new_stock_entry(tmp_path):
    from rhbot.portfolio import market_date
    r = _risk()
    p = Portfolio(10_000.0, str(tmp_path / "s.json"))
    p.day_trade_dates = [market_date()] * 3  # budget spent
    o = Order("A", AssetClass.STOCK, Side.BUY, 100.0)
    assert "PDT guard" in r.check(o, p, {})


def test_pdt_guard_ignores_crypto(tmp_path):
    from rhbot.portfolio import market_date
    r = _risk()
    p = Portfolio(10_000.0, str(tmp_path / "s.json"))
    p.day_trade_dates = [market_date()] * 5
    o = Order("BTC-USD", AssetClass.CRYPTO, Side.BUY, 100.0)
    assert r.check(o, p, {}) is None


def test_pdt_guard_never_blocks_an_exit(tmp_path):
    from rhbot.portfolio import market_date
    r = _risk()
    p = Portfolio(10_000.0, str(tmp_path / "s.json"))
    p.apply_fill(Fill("A", AssetClass.STOCK, Side.BUY, 10, 10.0))
    p.day_trade_dates = [market_date()] * 9  # way over budget
    o = Order("A", AssetClass.STOCK, Side.SELL, 100.0)
    assert r.check(o, p, {"A": 10.0}) is None  # exits stay allowed


def test_pdt_guard_can_be_disabled(tmp_path):
    from rhbot.portfolio import market_date
    r = _risk(pdt_guard=False)
    p = Portfolio(10_000.0, str(tmp_path / "s.json"))
    p.day_trade_dates = [market_date()] * 5
    o = Order("A", AssetClass.STOCK, Side.BUY, 100.0)
    assert r.check(o, p, {}) is None


def test_day_trade_dates_survive_restart(tmp_path):
    from rhbot.portfolio import market_date
    path = str(tmp_path / "s.json")
    p = Portfolio(10_000.0, path)
    p.apply_fill(Fill("A", AssetClass.STOCK, Side.BUY, 10, 10.0))
    p.apply_fill(Fill("A", AssetClass.STOCK, Side.SELL, 10, 11.0))
    p.save()
    q = Portfolio.load_or_new(10_000.0, path)
    assert q.day_trades_in_window() == 1


# ---- staleness guard ------------------------------------------------------

def _engine_with(bars_age_minutes, interval="1d"):
    """Build an Engine whose feed returns one bar of the given age."""
    from rhbot.config import Config, DashboardConfig, RiskConfig, WatchItem
    from rhbot.data.feed import DataFeed

    now = datetime.now(timezone.utc)
    bar = Bar(ts=now - timedelta(minutes=bars_age_minutes), open=1, high=1,
              low=1, close=1, volume=1)

    class OneBarFeed(DataFeed):
        def get_price(self, symbol): return 1.0
        def get_bars(self, symbol, count): return [bar]

    cfg = Config(
        mode="paper", bar_interval=interval,
        watchlist=[WatchItem("A", AssetClass.STOCK, "sma_crossover",
                             {"fast": 2, "slow": 4}, 100.0)],
        risk=RiskConfig(), dashboard=DashboardConfig(enabled=False),
    )
    from rhbot.engine import Engine
    feed = OneBarFeed()
    return Engine(cfg, feed, PaperBroker(feed),
                  Portfolio(1000.0, "/tmp/rhbot_test_state.json"),
                  RiskManager(cfg.risk))


def test_stale_bars_block_trading():
    e = _engine_with(bars_age_minutes=60 * 24 * 10)  # 10 days old
    assert e._is_stale("A", e.feed.get_bars("A", 5)) is True


def test_fresh_bars_allow_trading():
    e = _engine_with(bars_age_minutes=30)
    assert e._is_stale("A", e.feed.get_bars("A", 5)) is False


def test_empty_bars_are_stale():
    e = _engine_with(bars_age_minutes=1)
    assert e._is_stale("A", []) is True


def test_intraday_staleness_is_stricter_than_daily():
    from rhbot.config import BAR_INTERVALS
    assert BAR_INTERVALS["1m"][1] < BAR_INTERVALS["1d"][1]


# ---- Yahoo daily-bar completion -------------------------------------------
# Yahoo publishes the current session's daily bar with a null close until it
# settles, so a naive daily fetch lags a full session. These cover the repair.

def _stub_feed(daily_days, intraday_points):
    """YahooFeed with the network stubbed out."""
    from rhbot.data.yahoo_feed import YahooFeed
    base = datetime(2026, 7, 20, 20, 0, tzinfo=timezone.utc)

    daily = [Bar(ts=base + timedelta(days=d), open=10, high=11, low=9,
                 close=10 + d, volume=100) for d in daily_days]
    intra = [Bar(ts=base + timedelta(days=d, minutes=m), open=o, high=h,
                 low=lo, close=c, volume=5)
             for (d, m, o, h, lo, c) in intraday_points]

    f = YahooFeed(interval="1d", range_="2y")
    f._fetch_raw = lambda sym, interval, rng: (
        list(daily) if interval == "1d" else list(intra))
    return f


def test_daily_backfill_appends_current_session():
    # Daily data stops at day 0; intraday shows day 1 traded.
    f = _stub_feed([0], [(1, 1, 20.0, 25.0, 19.0, 21.0),
                         (1, 2, 21.0, 26.0, 20.0, 24.0)])
    bars = f.get_bars("X", 10)
    assert len(bars) == 2
    latest = bars[-1]
    assert latest.open == 20.0          # first intraday open
    assert latest.high == 26.0          # session high
    assert latest.low == 19.0           # session low
    assert latest.close == 24.0         # last intraday close
    assert latest.volume == 10          # summed


def test_daily_backfill_is_noop_when_current():
    # Intraday only covers a day already present in the daily series.
    f = _stub_feed([0, 1], [(1, 5, 1.0, 1.0, 1.0, 1.0)])
    assert len(f.get_bars("X", 10)) == 2


def test_daily_backfill_survives_intraday_failure():
    from rhbot.data.yahoo_feed import YahooFeed
    base = datetime(2026, 7, 20, 20, 0, tzinfo=timezone.utc)
    f = YahooFeed(interval="1d", range_="2y")
    f._fetch_raw = lambda sym, interval, rng: (
        [Bar(ts=base, open=1, high=1, low=1, close=1, volume=1)]
        if interval == "1d" else [])
    assert len(f.get_bars("X", 10)) == 1  # daily still usable


def test_price_reflects_completed_session():
    f = _stub_feed([0], [(1, 1, 20.0, 25.0, 19.0, 24.0)])
    assert f.get_price("X") == 24.0  # today's price, not yesterday's close


# ---- config validation ----------------------------------------------------

def test_bad_bar_interval_rejected(tmp_path):
    import yaml as _yaml
    from rhbot.config import load_config
    p = tmp_path / "c.yaml"
    p.write_text(_yaml.dump({
        "bar_interval": "7s",
        "watchlist": [{"symbol": "A", "asset_class": "stock",
                       "strategy": "sma_crossover"}],
    }))
    with pytest.raises(ValueError, match="bar_interval"):
        load_config(str(p))


def test_bar_age_limit_auto_and_override():
    from rhbot.config import Config
    assert Config(bar_interval="1m").bar_age_limit_minutes == 15
    assert Config(bar_interval="1m",
                  max_bar_age_minutes=99).bar_age_limit_minutes == 99


def test_backtest_cooldown_reduces_trades():
    closes = [10, 9, 10, 9, 10, 9, 10, 9, 10, 9, 10, 9, 10, 9, 10, 9, 10]
    bars = mkbars(closes)
    hot = run_backtest("hot", SlopeReversal({"smooth": 1}), bars,
                       cooldown_bars=0, bar_minutes=1.0)
    cold = run_backtest("cold", SlopeReversal({"smooth": 1}), bars,
                        cooldown_bars=10, bar_minutes=1.0)
    assert cold.trades <= hot.trades


def test_appreciated_winner_can_still_be_sold(tmp_path):
    """max_order_notional caps ENTRIES only.

    Exits are sized at current market value, so a position that grows past the
    cap would fail the size check and become impossible to close — the bot
    would be trapped in exactly its biggest winners.
    """
    r = _risk()  # max_order_notional=1000
    p = Portfolio(10_000.0, str(tmp_path / "s.json"))
    p.apply_fill(Fill("X", AssetClass.STOCK, Side.BUY, 10, 90.0))  # $900 entry
    # Position triples; the exit is now worth $2,700, well over the $1,000 cap.
    sell = Order("X", AssetClass.STOCK, Side.SELL, 2700.0)
    assert r.check(sell, p, {"X": 270.0}) is None


def test_oversized_entry_is_still_blocked(tmp_path):
    """The cap must keep working for buys — this is not a blanket exemption."""
    r = _risk()
    p = Portfolio(10_000.0, str(tmp_path / "s.json"))
    buy = Order("X", AssetClass.STOCK, Side.BUY, 2700.0)
    assert "max_order_notional" in r.check(buy, p, {})


# ---- marking untracked positions ------------------------------------------

def test_held_positions_are_priced_even_when_off_the_watchlist(tmp_path):
    """A position dropped from the watchlist must not mark at cost forever.

    Marking at cost overstates equity and feeds a wrong number to the
    daily-loss kill switch, which is the one guard that must not be blind.
    """
    from rhbot.config import Config, WatchItem
    from rhbot.engine import Engine

    cfg = Config(
        watchlist=[WatchItem("WATCHED", AssetClass.STOCK, "sma_crossover")],
        risk=RiskConfig(kill_switch_file="/nonexistent/STOP"))

    feed = SyntheticFeed(base_prices={"WATCHED": 50.0, "ORPHAN": 200.0})
    portfolio = Portfolio(10_000.0, str(tmp_path / "s.json"))
    # Bought at 100; the feed now marks it near 200.
    portfolio.apply_fill(Fill("ORPHAN", AssetClass.STOCK, Side.BUY, 10, 100.0))

    engine = Engine(cfg, feed, PaperBroker(feed), portfolio,
                    RiskManager(cfg.risk))
    prices = engine._refresh_prices()

    assert "ORPHAN" in prices, "held position was never priced"
    assert prices["ORPHAN"] != pytest.approx(100.0), "still marked at cost"


# ---- relearn must not report a network failure as a market finding ---------

def test_relearn_aborts_when_the_data_source_is_down():
    """Zero symbols studied is a broken fetch, not evidence about the strategy."""
    import relearn
    calls = {"n": 0}

    def dead_fetch(*a, **kw):
        calls["n"] += 1
        return None

    original = relearn.fetch_yahoo
    try:
        relearn.fetch_yahoo = dead_fetch
        assert relearn.wait_for_data(attempts=3, delay=0,
                                     sleep=lambda _: None) is False
        assert calls["n"] == 3, "should retry before giving up"
    finally:
        relearn.fetch_yahoo = original


def test_relearn_proceeds_once_data_answers():
    import relearn
    original = relearn.fetch_yahoo
    try:
        relearn.fetch_yahoo = lambda *a, **kw: [object()]
        assert relearn.wait_for_data(attempts=3, delay=0,
                                     sleep=lambda _: None) is True
    finally:
        relearn.fetch_yahoo = original


def test_survivors_filter_rejects_losers_and_small_samples():
    """Beating a -40% hold with -27% is still losing money."""
    import relearn
    rows = [
        {"symbol": "GOOD", "round_trips": 10, "test": 20.0, "edge": 5.0, "maxdd": 10.0},
        {"symbol": "LOSS", "round_trips": 10, "test": -5.0, "edge": 30.0, "maxdd": 10.0},
        {"symbol": "THIN", "round_trips": 2, "test": 50.0, "edge": 40.0, "maxdd": 10.0},
        {"symbol": "DEEP", "round_trips": 10, "test": 80.0, "edge": 40.0, "maxdd": 90.0},
        {"symbol": "ERR", "error": "boom"},
    ]
    assert [r["symbol"] for r in relearn.survivors(rows)] == ["GOOD"]


# ---- orphaned positions must stay sellable --------------------------------

def _orphan_engine(tmp_path, watch_symbols, held):
    from rhbot.config import Config, WatchItem
    from rhbot.engine import Engine
    cfg = Config(
        watchlist=[WatchItem(s, AssetClass.STOCK, "slope_reversal",
                             {"smooth": 2, "min_slope_pct": 0.0}, 100.0)
                   for s in watch_symbols],
        risk=RiskConfig(kill_switch_file="/nonexistent/STOP",
                        min_seconds_between_trades=0))
    feed = SyntheticFeed(base_prices={s: 100.0 for s in
                                      set(watch_symbols) | set(held)})
    p = Portfolio(100_000.0, str(tmp_path / "s.json"))
    for sym, qty in held.items():
        p.apply_fill(Fill(sym, AssetClass.STOCK, Side.BUY, qty, 100.0))
    return Engine(cfg, feed, PaperBroker(feed), p, RiskManager(cfg.risk)), p


def test_held_symbol_off_the_watchlist_is_still_evaluated(tmp_path):
    """Otherwise it can never be sold — no code path ever asks for its exit."""
    engine, _ = _orphan_engine(tmp_path, ["WATCHED"], {"ORPHAN": 10.0})
    seen = []
    engine._process_symbol = lambda s, p: seen.append(s)
    engine.tick()
    assert "ORPHAN" in seen, "held position was never evaluated"
    assert "WATCHED" in seen


def test_orphan_gets_an_exit_only_strategy(tmp_path):
    engine, _ = _orphan_engine(tmp_path, ["WATCHED"], {"ORPHAN": 10.0})
    strat, asset_class = engine._orphan("ORPHAN")
    assert strat is not None
    assert asset_class == AssetClass.STOCK


def test_orphan_is_never_bought_again(tmp_path):
    """Exiting a dropped position is right; re-entering one is not."""
    from rhbot.models import Signal, SignalType
    engine, portfolio = _orphan_engine(tmp_path, ["WATCHED"], {"ORPHAN": 10.0})
    submitted = []
    engine.broker.submit = lambda o: submitted.append(o)

    strat, _ = engine._orphan("ORPHAN")
    strat.evaluate = lambda *a, **kw: Signal("ORPHAN", SignalType.ENTER_LONG, "x")
    engine._process_symbol("ORPHAN", {"ORPHAN": 100.0})
    assert submitted == [], "re-entered a symbol that left the watchlist"


def test_orphan_exit_is_submitted(tmp_path):
    from rhbot.models import Signal, SignalType
    engine, portfolio = _orphan_engine(tmp_path, ["WATCHED"], {"ORPHAN": 10.0})
    submitted = []
    engine.broker.submit = lambda o: submitted.append(o) or None

    strat, _ = engine._orphan("ORPHAN")
    strat.evaluate = lambda *a, **kw: Signal("ORPHAN", SignalType.EXIT_LONG, "x")
    engine._process_symbol("ORPHAN", {"ORPHAN": 100.0})
    assert len(submitted) == 1 and submitted[0].side == Side.SELL


# ---- a daily-loss halt must not trap you in a position ---------------------

def test_daily_loss_halt_blocks_entries(tmp_path):
    r = _risk(max_daily_loss=-100)
    p = Portfolio(10_000.0, str(tmp_path / "s.json"))
    p.day_start_equity = 10_000.0
    r.check_global_halts(p, {})
    r._halt("daily loss", r.HALT_ENTRIES_ONLY)
    o = Order("X", AssetClass.STOCK, Side.BUY, 100.0)
    assert "no new entries" in r.check(o, p, {})


def test_daily_loss_halt_still_allows_exits(tmp_path):
    """It fires when losing — the moment you most need to be able to sell."""
    r = _risk(max_daily_loss=-100)
    p = Portfolio(10_000.0, str(tmp_path / "s.json"))
    p.apply_fill(Fill("X", AssetClass.STOCK, Side.BUY, 10, 10.0))
    r._halt("daily loss", r.HALT_ENTRIES_ONLY)
    o = Order("X", AssetClass.STOCK, Side.SELL, 100.0)
    assert r.check(o, p, {"X": 10.0}) is None


def test_kill_switch_blocks_exits_too(tmp_path):
    """A human freeze is different: it means stop everything, deliberately."""
    r = _risk()
    p = Portfolio(10_000.0, str(tmp_path / "s.json"))
    p.apply_fill(Fill("X", AssetClass.STOCK, Side.BUY, 10, 10.0))
    r._halt("kill switch", r.HALT_EVERYTHING)
    o = Order("X", AssetClass.STOCK, Side.SELL, 100.0)
    assert "trading halted" in r.check(o, p, {"X": 10.0})


def test_daily_loss_does_not_stop_the_tick(tmp_path):
    """check_global_halts must return False so exits still get evaluated."""
    cfg = RiskConfig(max_daily_loss=-1.0, kill_switch_file="/nonexistent/STOP")
    r = RiskManager(cfg)
    p = Portfolio(1_000.0, str(tmp_path / "s.json"))
    p.apply_fill(Fill("X", AssetClass.STOCK, Side.BUY, 10, 100.0))
    stopped = r.check_global_halts(p, {"X": 50.0})   # big loss
    assert stopped is False, "tick aborted; exits would never be considered"
    assert r.halted() is True
    assert r.blocks_exits() is False


# ---- time-based exit -------------------------------------------------------

def _hold_engine(tmp_path, max_hold_days):
    from rhbot.config import Config, WatchItem
    from rhbot.engine import Engine
    cfg = Config(
        watchlist=[WatchItem("X", AssetClass.STOCK, "slope_reversal",
                             {"smooth": 2, "min_slope_pct": 0.0}, 100.0,
                             max_hold_days=max_hold_days)],
        risk=RiskConfig(kill_switch_file="/nonexistent/STOP",
                        min_seconds_between_trades=0))
    feed = SyntheticFeed(base_prices={"X": 100.0})
    p = Portfolio(10_000.0, str(tmp_path / "s.json"))
    return Engine(cfg, feed, PaperBroker(feed), p, RiskManager(cfg.risk)), p


def test_position_records_when_it_opened(tmp_path):
    p = Portfolio(10_000.0, str(tmp_path / "s.json"))
    p.apply_fill(Fill("X", AssetClass.STOCK, Side.BUY, 10, 100.0))
    assert p.positions["X"].opened_ts is not None


def test_adding_to_a_position_does_not_reset_its_age(tmp_path):
    """Otherwise topping up restarts the clock and the cap never fires."""
    p = Portfolio(10_000.0, str(tmp_path / "s.json"))
    first = datetime.now(timezone.utc) - timedelta(days=5)
    p.apply_fill(Fill("X", AssetClass.STOCK, Side.BUY, 10, 100.0, ts=first))
    stamped = p.positions["X"].opened_ts
    p.apply_fill(Fill("X", AssetClass.STOCK, Side.BUY, 10, 101.0))
    assert p.positions["X"].opened_ts == stamped


def test_closing_clears_the_age(tmp_path):
    p = Portfolio(10_000.0, str(tmp_path / "s.json"))
    p.apply_fill(Fill("X", AssetClass.STOCK, Side.BUY, 10, 100.0))
    p.apply_fill(Fill("X", AssetClass.STOCK, Side.SELL, 10, 101.0))
    assert p.positions["X"].opened_ts is None


def test_age_survives_a_restart(tmp_path):
    path = str(tmp_path / "s.json")
    p = Portfolio(10_000.0, path)
    p.apply_fill(Fill("X", AssetClass.STOCK, Side.BUY, 10, 100.0))
    p.save()
    assert Portfolio.load_or_new(10_000.0, path).positions["X"].opened_ts \
        == p.positions["X"].opened_ts


def test_old_position_is_force_exited(tmp_path):
    engine, p = _hold_engine(tmp_path, max_hold_days=2)
    old = datetime.now(timezone.utc) - timedelta(days=3)
    p.apply_fill(Fill("X", AssetClass.STOCK, Side.BUY, 10, 100.0, ts=old))
    submitted = []
    engine.broker.submit = lambda o: submitted.append(o) or None
    engine._process_symbol("X", {"X": 100.0})
    assert len(submitted) == 1 and submitted[0].side == Side.SELL


def test_young_position_is_left_alone(tmp_path):
    engine, p = _hold_engine(tmp_path, max_hold_days=2)
    p.apply_fill(Fill("X", AssetClass.STOCK, Side.BUY, 10, 100.0))
    submitted = []
    engine.broker.submit = lambda o: submitted.append(o) or None
    engine._process_symbol("X", {"X": 100.0})
    assert submitted == []


def test_no_limit_configured_means_no_forced_exit(tmp_path):
    engine, p = _hold_engine(tmp_path, max_hold_days=None)
    old = datetime.now(timezone.utc) - timedelta(days=99)
    p.apply_fill(Fill("X", AssetClass.STOCK, Side.BUY, 10, 100.0, ts=old))
    submitted = []
    engine.broker.submit = lambda o: submitted.append(o) or None
    engine._process_symbol("X", {"X": 100.0})
    assert submitted == []


def test_missing_timestamp_never_forces_an_exit(tmp_path):
    """A blank stamp must not read as 'infinitely old'."""
    engine, p = _hold_engine(tmp_path, max_hold_days=2)
    p.apply_fill(Fill("X", AssetClass.STOCK, Side.BUY, 10, 100.0))
    p.positions["X"].opened_ts = None
    submitted = []
    engine.broker.submit = lambda o: submitted.append(o) or None
    engine._process_symbol("X", {"X": 100.0})
    assert submitted == []
