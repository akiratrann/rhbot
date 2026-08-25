"""Tests for the trend-following strategy.

The behaviour that matters: it must NOT sell into strength (that is the bug the
mean-reversion strategies have in a bull market) and it must actually let go
when the trend breaks.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from rhbot.models import AssetClass, Bar, Position, SignalType
from rhbot.strategy import build_strategy
from rhbot.strategy.trend_follow import TrendFollow, average_range


def mkbars(closes, spread=1.0):
    now = datetime.now(timezone.utc)
    return [Bar(ts=now - timedelta(days=len(closes) - i),
                open=c, high=c + spread, low=c - spread, close=c, volume=1.0)
            for i, c in enumerate(closes)]


def long_position(qty=10.0, avg=100.0):
    return Position(symbol="X", asset_class=AssetClass.STOCK,
                    quantity=qty, avg_price=avg)


def _strategy(**params):
    base = {"fast": 3, "slow": 6, "exit_ma": 4}
    base.update(params)
    return TrendFollow(base)


# ---- construction ---------------------------------------------------------

def test_registered_under_its_config_name():
    assert isinstance(build_strategy("trend_follow", {}), TrendFollow)


def test_fast_must_be_shorter_than_slow():
    with pytest.raises(ValueError, match="must be shorter"):
        TrendFollow({"fast": 50, "slow": 20})


def test_warmup_covers_the_longest_window():
    assert TrendFollow({"fast": 10, "slow": 100, "exit_ma": 50}).warmup_bars > 100


def test_holds_during_warmup():
    s = _strategy()
    assert s.evaluate("X", mkbars([1, 2, 3]), None).type == SignalType.HOLD


# ---- entries --------------------------------------------------------------

def test_enters_when_price_leads_both_averages():
    s = _strategy()
    rising = [10, 11, 12, 13, 14, 15, 16, 17, 18, 20]
    assert s.evaluate("X", mkbars(rising), None).type == SignalType.ENTER_LONG


def test_no_entry_in_a_downtrend():
    s = _strategy()
    falling = [20, 19, 18, 17, 16, 15, 14, 13, 12, 10]
    assert s.evaluate("X", mkbars(falling), None).type == SignalType.HOLD


def test_no_entry_when_fast_is_below_slow():
    """A pop above the fast MA inside a downtrend is not a confirmed trend."""
    s = _strategy()
    bars = mkbars([30, 28, 26, 24, 22, 20, 18, 16, 14, 15.5])
    assert s.evaluate("X", bars, None).type == SignalType.HOLD


# ---- the point of the strategy -------------------------------------------

def test_does_not_sell_into_strength():
    """The whole reason this exists: mean reversion exits winners too early."""
    s = _strategy()
    climbing = [10, 12, 14, 16, 18, 20, 22, 24, 26, 30]
    sig = s.evaluate("X", mkbars(climbing), long_position())
    assert sig.type == SignalType.HOLD


def test_exits_when_price_breaks_the_exit_average():
    s = _strategy()
    rolled_over = [20, 21, 22, 23, 24, 25, 24, 20, 15, 8]
    sig = s.evaluate("X", mkbars(rolled_over), long_position())
    assert sig.type == SignalType.EXIT_LONG


# ---- volatility band ------------------------------------------------------

def test_average_range_is_the_mean_high_low_spread():
    assert average_range(mkbars([10, 10, 10], spread=2.0), 3) == pytest.approx(4.0)


def test_average_range_needs_enough_bars():
    assert average_range(mkbars([10, 10]), 5) is None


def test_stop_band_tolerates_a_dip_that_a_bare_ma_would_not():
    """A wide band should keep a position that the unbuffered exit would drop."""
    dipping = [20, 21, 22, 23, 24, 25, 24, 23, 21.5, 21.0]
    bars = mkbars(dipping, spread=3.0)

    tight = _strategy(stop_atr_mult=0.0).evaluate("X", bars, long_position())
    wide = _strategy(stop_atr_mult=5.0, range_period=3).evaluate(
        "X", bars, long_position())

    assert tight.type == SignalType.EXIT_LONG
    assert wide.type == SignalType.HOLD


def test_flat_position_object_is_treated_as_no_position():
    """quantity=0 must not be mistaken for holding, or exits fire forever."""
    s = _strategy()
    rising = [10, 11, 12, 13, 14, 15, 16, 17, 18, 20]
    sig = s.evaluate("X", mkbars(rising), long_position(qty=0.0))
    assert sig.type == SignalType.ENTER_LONG


# ---- least-squares derivative ---------------------------------------------

def test_regression_slope_recovers_a_known_line():
    from rhbot.strategy.slope_regression import regression_slope
    assert regression_slope([1, 4, 7, 10]) == pytest.approx(3.0)
    assert regression_slope([10, 7, 4, 1]) == pytest.approx(-3.0)
    assert regression_slope([5, 5, 5, 5]) == pytest.approx(0.0)


def test_regression_slope_is_steadier_than_a_two_point_difference():
    """The whole premise: differencing amplifies noise, regression averages it."""
    from rhbot.strategy.slope_regression import regression_slope
    clean = [100 + i for i in range(10)]                    # true slope = 1
    noisy = [v + (2 if i % 2 else -2) for i, v in enumerate(clean)]
    two_point = noisy[-1] - noisy[-2]
    reg = regression_slope(noisy)
    assert abs(reg - 1.0) < abs(two_point - 1.0)


def test_regression_window_must_be_sane():
    from rhbot.strategy.slope_regression import SlopeRegression
    with pytest.raises(ValueError, match="window must be"):
        SlopeRegression({"window": 2})


def test_registered_under_its_config_name():
    from rhbot.strategy import build_strategy
    from rhbot.strategy.slope_regression import SlopeRegression
    assert isinstance(build_strategy("slope_regression", {}), SlopeRegression)
