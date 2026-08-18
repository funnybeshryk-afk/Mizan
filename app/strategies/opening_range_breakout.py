from __future__ import annotations

from typing import Any

from app.database.models import SignalAction
from app.market.provider import Bar
from app.market.session import minutes_since_session_open, to_eastern
from app.strategies.strategy import Signal, Strategy

DEFAULT_PARAMS: dict[str, Any] = {
    "opening_range_minutes": 15,
}


def _opening_range(session_bars: list[Bar], opening_range_minutes: int):
    """The high/low of session_bars' own bars from the session open through
    +opening_range_minutes, plus whether that window has fully elapsed yet
    (judged by the most recent bar's own timestamp, not bar count - a
    coarser timeframe can need fewer bars to cover the same wall-clock
    window than a finer one). Returns (high, low, is_formed).

    The window is strictly BEFORE opening_range_minutes and "formed"
    requires the current (most recent) bar to be AT OR AFTER it - the same
    no-lookahead shape as Donchian/Bollinger excluding the current bar
    from their own window, just keyed by session-clock time instead of
    bar count.
    """
    if not session_bars:
        return None, None, False

    current_bar = session_bars[-1]
    if minutes_since_session_open(current_bar.timestamp) < opening_range_minutes:
        return None, None, False

    window_bars = [
        bar for bar in session_bars if minutes_since_session_open(bar.timestamp) < opening_range_minutes
    ]
    if not window_bars:
        return None, None, False

    high = max(bar.high for bar in window_bars)
    low = min(bar.low for bar in window_bars)
    return high, low, True


class OpeningRangeBreakoutStrategy(Strategy):
    """Opening Range Breakout (ORB), long-only, intraday.

    The opening range for a trading day is the high/low of that day's own
    bars from the session open (09:30 ET) through +opening_range_minutes -
    a window anchored to session clock time, not "the last N bars": the
    number of bars covering 15 minutes depends on the bot's own timeframe
    (many for "1Min", one or zero for "1Hour"), and the range must reset
    every trading day regardless of how much bar history precedes it.
    Bars are grouped into "today's session" by their own Eastern-local
    calendar date (see app.market.session.to_eastern) - a bar timestamp
    from a previous day never leaks into today's range.

    BUY when the close breaks strictly above the current session's opening
    range high, once that range has fully formed (warm-up: WAIT until
    opening_range_minutes have elapsed since the open, same idea as every
    other strategy's warm-up, just measured in session-clock time instead
    of bar count). SELL (close) when the close falls back strictly below
    that same high - a failed breakout. The range's low is computed but,
    in this first version, doesn't drive a signal on its own; long-only,
    same as every other strategy here.

    Like TrendStrategy/BreakoutStrategy/MeanReversionStrategy, this is a
    pure, stateless function of the bars it's given - no-pyramiding (don't
    BUY again while already long) and no-shorting are NOT its job, they're
    enforced by the same caller-side checks every other strategy already
    relies on (app.automation.daemon._execute_signal, app.backtesting.
    engine's execution helpers). The opening range high is fixed for the
    whole session, so once broken out, every later bar that stays above it
    keeps yielding BUY too - exactly like BreakoutStrategy's rolling
    channel can for several consecutive bars; it's the caller's BUY-while-
    long no-op that prevents a duplicate position, not internal state here.
    """

    def __init__(self, params: dict[str, Any] | None = None):
        merged = dict(DEFAULT_PARAMS)
        merged.update(params or {})
        super().__init__(merged)

    def generate_signal(self, bars: list[Bar]) -> Signal:
        last_price = bars[-1].close if bars else None
        if not bars:
            return Signal(action=SignalAction.WAIT, price=last_price)

        opening_range_minutes = self.params["opening_range_minutes"]
        current_bar = bars[-1]
        session_day = to_eastern(current_bar.timestamp).date()
        session_bars = [bar for bar in bars if to_eastern(bar.timestamp).date() == session_day]

        range_high, _range_low, range_is_formed = _opening_range(session_bars, opening_range_minutes)

        if not range_is_formed:
            return Signal(action=SignalAction.WAIT, price=last_price)

        if last_price > range_high:
            return Signal(action=SignalAction.BUY, price=last_price)
        if last_price < range_high:
            return Signal(action=SignalAction.SELL, price=last_price)
        return Signal(action=SignalAction.WAIT, price=last_price)
