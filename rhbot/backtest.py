"""Backtester — replays a strategy over historical bars and scores it.

This is how you answer "what cooldown / smoothing should I use?" empirically
instead of guessing. It models costs that kill high-frequency ideas:
  - slippage (bps per side)
  - a per-symbol cooldown, in BARS (the frequency governor)
  - day-trade counting, so you can see if a config would breach PDT

Metrics reported: total return, trade count, day-trade count, win rate, max
drawdown, and trades/week. Past performance does not predict future results —
this tells you how a rule BEHAVED, not how it WILL behave.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional, Tuple

from .models import AssetClass, Bar, Position, SignalType
from .strategy.base import Strategy


@dataclass
class BacktestResult:
    label: str
    start_equity: float
    end_equity: float
    trades: int
    round_trips: int
    wins: int
    day_trades: int
    max_drawdown_pct: float
    bars: int
    bar_minutes: float
    #: (timestamp, mark-to-market equity) at every evaluated bar. Needed to ask
    #: "what would a typical WEEK have looked like" — a single total return
    #: hides whether it arrived smoothly or in one lucky jump.
    equity_curve: List[Tuple[datetime, float]] = field(default_factory=list)

    @property
    def return_pct(self) -> float:
        if self.start_equity == 0:
            return 0.0
        return (self.end_equity / self.start_equity - 1.0) * 100.0

    @property
    def win_rate(self) -> float:
        return (self.wins / self.round_trips * 100.0) if self.round_trips else 0.0

    @property
    def weeks(self) -> float:
        total_minutes = self.bars * self.bar_minutes
        return max(total_minutes / (60 * 24 * 7), 1e-9)

    @property
    def trades_per_week(self) -> float:
        return self.trades / self.weeks

    @property
    def max_day_trades_per_week(self) -> float:
        return self.day_trades / self.weeks


def run_backtest(
    label: str,
    strategy: Strategy,
    bars: List[Bar],
    *,
    starting_cash: float = 10_000.0,
    order_notional: float = 1_000.0,
    slippage_bps: float = 2.0,
    cooldown_bars: int = 0,
    bar_minutes: float = 1.0,
    symbol: str = "TEST",
) -> BacktestResult:
    """Replay `strategy` over `bars`, one bar at a time, long-only."""
    cash = starting_cash
    position: Optional[Position] = None
    slip = slippage_bps / 10_000.0

    trades = 0
    wins = 0
    round_trips = 0
    day_trades = 0
    last_trade_idx: Optional[int] = None
    entry_price = 0.0
    entry_day: Optional[str] = None

    peak_equity = starting_cash
    max_dd = 0.0
    equity_curve: List[Tuple[datetime, float]] = []

    warmup = max(strategy.warmup_bars, 2)

    for i in range(warmup, len(bars)):
        window = bars[: i + 1]
        price = window[-1].close
        signal = strategy.evaluate(symbol, window, position)

        # Mark-to-market equity + drawdown tracking.
        equity = cash + (position.quantity * price if position else 0.0)
        equity_curve.append((window[-1].ts, equity))
        peak_equity = max(peak_equity, equity)
        if peak_equity > 0:
            dd = (peak_equity - equity) / peak_equity * 100.0
            max_dd = max(max_dd, dd)

        if signal.type == SignalType.HOLD:
            continue

        # Frequency governor, in bars.
        if (cooldown_bars > 0 and last_trade_idx is not None
                and (i - last_trade_idx) < cooldown_bars):
            continue

        day = window[-1].ts.strftime("%Y-%m-%d")

        if signal.type == SignalType.ENTER_LONG and position is None:
            fill = price * (1 + slip)
            notional = min(order_notional, cash)
            if notional <= 0:
                continue
            qty = notional / fill
            cash -= notional
            position = Position(symbol=symbol, asset_class=AssetClass.STOCK,
                                quantity=qty, avg_price=fill)
            entry_price = fill
            entry_day = day
            trades += 1
            last_trade_idx = i

        elif signal.type == SignalType.EXIT_LONG and position is not None:
            fill = price * (1 - slip)
            cash += position.quantity * fill
            round_trips += 1
            if fill > entry_price:
                wins += 1
            if entry_day == day:
                day_trades += 1  # bought and sold same day => PDT-countable
            position = None
            trades += 1
            last_trade_idx = i

    final_price = bars[-1].close if bars else 0.0
    end_equity = cash + (position.quantity * final_price if position else 0.0)

    return BacktestResult(
        label=label,
        start_equity=starting_cash,
        end_equity=end_equity,
        trades=trades,
        round_trips=round_trips,
        wins=wins,
        day_trades=day_trades,
        max_drawdown_pct=max_dd,
        bars=len(bars),
        bar_minutes=bar_minutes,
        equity_curve=equity_curve,
    )


def format_table(results: List[BacktestResult]) -> str:
    """Render results as a fixed-width table."""
    head = (f"{'Config':<38} {'Return%':>8} {'Trades':>7} {'Trd/wk':>7} "
            f"{'DayTr/wk':>9} {'Win%':>6} {'MaxDD%':>7}")
    lines = [head, "-" * len(head)]
    for r in results:
        lines.append(
            f"{r.label:<38} {r.return_pct:>8.2f} {r.trades:>7} "
            f"{r.trades_per_week:>7.1f} {r.max_day_trades_per_week:>9.1f} "
            f"{r.win_rate:>6.1f} {r.max_drawdown_pct:>7.2f}"
        )
    return "\n".join(lines)
