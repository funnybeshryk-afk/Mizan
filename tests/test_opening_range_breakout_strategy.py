from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from app.backtesting.engine import run_backtest
from app.database.models import SignalAction
from app.market.provider import Bar
from app.strategies.opening_range_breakout import OpeningRangeBreakoutStrategy

# Mid-July -> EDT (UTC-4), so 09:30 ET is 13:30 UTC. Picking a date outside
# any DST transition keeps the arithmetic in this file simple; the EST/EDT
# switch itself is app.market.session's job, not re-tested here.
DAY_1 = date(2024, 7, 15)
DAY_2 = date(2024, 7, 16)
SESSION_OPEN_UTC = (13, 30)


def _session_ts(day: date, minute_offset: int) -> datetime:
    hour, minute = SESSION_OPEN_UTC
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=timezone.utc) + timedelta(
        minutes=minute_offset
    )


def _opening_window_bars(day: date, high, low, count: int = 15) -> list[Bar]:
    """count 1-minute bars starting at the session open, all sharing the
    same high/low - the simplest shape that gives a known opening range."""
    high = Decimal(str(high))
    low = Decimal(str(low))
    mid = (high + low) / 2
    return [
        Bar(timestamp=_session_ts(day, i), open=mid, high=high, low=low, close=mid, volume=1000)
        for i in range(count)
    ]


def _bar_at(day: date, minute_offset: int, close) -> Bar:
    price = Decimal(str(close))
    return Bar(
        timestamp=_session_ts(day, minute_offset),
        open=price,
        high=price,
        low=price,
        close=price,
        volume=1000,
    )


# --- opening range boundary ----------------------------------------------------


def test_exactly_at_opening_range_high_is_not_a_breakout():
    strategy = OpeningRangeBreakoutStrategy()  # opening_range_minutes=15
    bars = _opening_window_bars(DAY_1, high=101, low=99) + [_bar_at(DAY_1, 15, 101)]

    signal = strategy.generate_signal(bars)

    assert signal.action == SignalAction.WAIT


def test_one_tick_above_opening_range_high_is_a_breakout():
    strategy = OpeningRangeBreakoutStrategy()
    bars = _opening_window_bars(DAY_1, high=101, low=99) + [_bar_at(DAY_1, 15, "101.01")]

    signal = strategy.generate_signal(bars)

    assert signal.action == SignalAction.BUY
    assert signal.price == Decimal("101.01")


def test_close_falling_back_below_the_range_high_yields_sell():
    strategy = OpeningRangeBreakoutStrategy()
    bars = _opening_window_bars(DAY_1, high=101, low=99) + [
        _bar_at(DAY_1, 15, "102"),  # breakout
        _bar_at(DAY_1, 16, "100.99"),  # failed breakout
    ]

    signal = strategy.generate_signal(bars)

    assert signal.action == SignalAction.SELL
    assert signal.price == Decimal("100.99")


# --- warm-up: opening range not yet formed --------------------------------------


def test_insufficient_session_time_yields_wait_even_with_a_high_close():
    """Only 11 minutes have elapsed since the open (opening_range_minutes=15)
    - the range can't be considered formed yet, regardless of price."""
    strategy = OpeningRangeBreakoutStrategy()
    bars = _opening_window_bars(DAY_1, high=101, low=99, count=10) + [_bar_at(DAY_1, 10, "500")]

    signal = strategy.generate_signal(bars)

    assert signal.action == SignalAction.WAIT


def test_no_bars_yields_wait_with_no_price():
    strategy = OpeningRangeBreakoutStrategy()

    signal = strategy.generate_signal([])

    assert signal.action == SignalAction.WAIT
    assert signal.price is None


# --- resets every trading day ----------------------------------------------------


def test_opening_range_is_recomputed_for_a_new_trading_day_not_carried_over():
    strategy = OpeningRangeBreakoutStrategy()
    day_1_bars = _opening_window_bars(DAY_1, high=101, low=99) + [_bar_at(DAY_1, 15, "102")]
    day_2_window = _opening_window_bars(DAY_2, high=105, low=103)

    # A close of 102 broke DAY_1's range (high=101) but is below DAY_2's own
    # range (high=105) - if DAY_2's evaluation wrongly used yesterday's
    # range, this would incorrectly come out as BUY.
    below_day_2_range = strategy.generate_signal(day_1_bars + day_2_window + [_bar_at(DAY_2, 15, "102")])
    assert below_day_2_range.action != SignalAction.BUY

    # And DAY_2's own range does drive a real breakout when actually broken.
    above_day_2_range = strategy.generate_signal(
        day_1_bars + day_2_window + [_bar_at(DAY_2, 15, "105.01")]
    )
    assert above_day_2_range.action == SignalAction.BUY


# --- params ----------------------------------------------------------------------


def test_default_params_are_15_minutes():
    strategy = OpeningRangeBreakoutStrategy()

    assert strategy.params == {"opening_range_minutes": 15}


def test_custom_opening_range_minutes_is_used_not_hardcoded():
    strategy = OpeningRangeBreakoutStrategy({"opening_range_minutes": 5})
    bars = _opening_window_bars(DAY_1, high=101, low=99, count=5) + [_bar_at(DAY_1, 5, "101.01")]

    signal = strategy.generate_signal(bars)

    assert signal.action == SignalAction.BUY
    assert strategy.params["opening_range_minutes"] == 5


def test_generate_signal_never_touches_broker_or_portfolio():
    strategy = OpeningRangeBreakoutStrategy()
    assert not hasattr(strategy, "submit_order")
    assert not hasattr(strategy, "buy")
    assert not hasattr(strategy, "sell")


# --- no pyramiding (backtest engine's job, not the strategy's) -------------------


def test_backtest_does_not_pyramid_while_price_stays_above_the_range_high():
    """The opening range high is fixed for the whole session, so a
    sustained rally keeps yielding BUY on every bar after the breakout
    (generate_signal is stateless - it has no idea a position is already
    open). The backtest engine's existing all-in/all-out execution must
    still open exactly one position, exactly like BreakoutStrategy's own
    equivalent test.
    """
    strategy = OpeningRangeBreakoutStrategy()
    bars = _opening_window_bars(DAY_1, high=101, low=99) + [
        _bar_at(DAY_1, 15, "102"),
        _bar_at(DAY_1, 16, "103"),
        _bar_at(DAY_1, 17, "104"),
        _bar_at(DAY_1, 18, "105"),
    ]

    result = run_backtest(strategy, bars, initial_capital=Decimal("10000"), symbol="TEST")

    assert result.num_trades == 1
