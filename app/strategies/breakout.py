from __future__ import annotations

from typing import Any

from app.database.models import SignalAction
from app.market.provider import Bar
from app.strategies.indicators import donchian_lower, donchian_upper
from app.strategies.strategy import Signal, Strategy

DEFAULT_PARAMS: dict[str, Any] = {
    "entry_period": 20,
    "exit_period": 10,
}


class BreakoutStrategy(Strategy):
    """Donchian channel breakout, long-only (Turtle Trading System 1).

    BUY when the close breaks strictly above the highest close of the
    preceding entry_period bars - a new high, enter the trend. SELL
    (close an existing long - this never opens a short) when the close
    breaks strictly below the lowest close of the preceding exit_period
    bars - a tighter, faster channel than the entry one, so a give-back
    exits before the entry channel would even notice.

    Like RiskManager's stop-loss/take-profit, no-pyramiding (don't BUY
    again while already long) and no-shorting (don't SELL while flat) are
    NOT this strategy's job - it's a pure, stateless function of the bars
    it's given, same contract as TrendStrategy. Those rules already live
    where TrendStrategy's do: app.backtesting.engine's execution helpers
    and app.automation.daemon._execute_signal.

    entry_period >= exit_period (true for the documented defaults, 20/10)
    guarantees the two channels can never both trigger on the same bar:
    the exit_period window is a subset of the entry_period window, so the
    entry (upper) channel is always >= the exit (lower) channel - being
    above the upper and below the lower at once would be a contradiction.
    """

    def __init__(self, params: dict[str, Any] | None = None):
        merged = dict(DEFAULT_PARAMS)
        merged.update(params or {})
        super().__init__(merged)

    def generate_signal(self, bars: list[Bar]) -> Signal:
        closes = [bar.close for bar in bars]
        last_price = closes[-1] if closes else None

        entry_period = self.params["entry_period"]
        exit_period = self.params["exit_period"]

        # entry_period closes strictly before the current one are needed
        # for a fully-populated entry channel - entry_period + 1 closes
        # total. With entry_period >= exit_period this also guarantees
        # the exit channel is ready.
        if len(closes) < entry_period + 1:
            return Signal(action=SignalAction.WAIT, price=last_price)

        upper = donchian_upper(closes, entry_period)[-1]
        lower = donchian_lower(closes, exit_period)[-1]

        if upper is not None and last_price > upper:
            return Signal(action=SignalAction.BUY, price=last_price)
        if lower is not None and last_price < lower:
            return Signal(action=SignalAction.SELL, price=last_price)
        return Signal(action=SignalAction.WAIT, price=last_price)
