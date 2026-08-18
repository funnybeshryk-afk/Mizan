from __future__ import annotations

from typing import Any

from app.database.models import SignalAction
from app.market.provider import Bar
from app.strategies.indicators import bollinger_lower, bollinger_middle
from app.strategies.strategy import Signal, Strategy

DEFAULT_PARAMS: dict[str, Any] = {
    "period": 20,
    "num_std": 2.0,
}


class MeanReversionStrategy(Strategy):
    """Bollinger Band mean reversion, long-only.

    BUY when the close breaks strictly below the lower band (the
    period-bar SMA minus num_std standard deviations, both computed from
    the preceding `period` bars only - the current bar is never part of
    its own band, same convention as BreakoutStrategy's Donchian
    channels) - oversold, expect a reversion back up. SELL (close an
    existing long - this never opens a short) when the close climbs back
    strictly above the middle band (the SMA itself) - the classic
    mean-reversion exit on returning to the mean, not waiting for the
    upper band.

    Same contract as TrendStrategy/BreakoutStrategy: a pure, stateless
    function of the bars it's given. No-pyramiding, no-shorting, and any
    stop-loss/take-profit are not this strategy's job - those live where
    they already do for every other strategy: app.backtesting.engine's
    execution helpers and app.automation.daemon._execute_signal, gated by
    RiskManager, not reimplemented here.

    lower <= middle always (num_std and the stddev are both >= 0), so
    "below the lower band" and "above the middle band" can never both be
    true on the same bar - the same mutual-exclusivity BreakoutStrategy's
    entry_period >= exit_period guarantees for its own two channels.
    """

    def __init__(self, params: dict[str, Any] | None = None):
        merged = dict(DEFAULT_PARAMS)
        merged.update(params or {})
        super().__init__(merged)

    def generate_signal(self, bars: list[Bar]) -> Signal:
        closes = [bar.close for bar in bars]
        last_price = closes[-1] if closes else None

        period = self.params["period"]
        num_std = self.params["num_std"]

        # period closes strictly before the current one are needed for a
        # fully-populated band - period + 1 closes total, same off-by-one
        # reasoning as BreakoutStrategy's entry_period + 1.
        if len(closes) < period + 1:
            return Signal(action=SignalAction.WAIT, price=last_price)

        lower = bollinger_lower(closes, period, num_std)[-1]
        middle = bollinger_middle(closes, period)[-1]

        if lower is not None and last_price < lower:
            return Signal(action=SignalAction.BUY, price=last_price)
        if middle is not None and last_price > middle:
            return Signal(action=SignalAction.SELL, price=last_price)
        return Signal(action=SignalAction.WAIT, price=last_price)
