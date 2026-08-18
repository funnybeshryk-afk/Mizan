from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

from app.backtesting.engine import run_backtest
from app.database.models import SignalAction
from app.market.provider import Bar
from app.strategies.breakout import BreakoutStrategy

SMALL_PARAMS = {"entry_period": 5, "exit_period": 3}


def _bars_from_closes(closes):
    ts = datetime(2024, 1, 1, tzinfo=timezone.utc)
    bars = []
    for i, close in enumerate(closes):
        price = Decimal(str(close))
        bars.append(
            Bar(timestamp=ts + timedelta(days=i), open=price, high=price, low=price, close=price, volume=1000)
        )
    return bars


# --- entry (upper) channel -----------------------------------------------------


def test_breakout_above_entry_channel_yields_buy():
    # entry_period=5: prior closes 100,101,99,102,100 -> upper channel = 102
    strategy = BreakoutStrategy(SMALL_PARAMS)
    signal = strategy.generate_signal(_bars_from_closes([100, 101, 99, 102, 100, 103]))

    assert signal.action == SignalAction.BUY
    assert signal.price == Decimal("103")


def test_exactly_at_upper_boundary_is_not_a_breakout():
    """Current close equal to (not above) the prior max must not trigger -
    the condition is a strict breakout, not a touch."""
    strategy = BreakoutStrategy(SMALL_PARAMS)
    signal = strategy.generate_signal(_bars_from_closes([100, 101, 99, 102, 100, 102]))

    assert signal.action == SignalAction.WAIT


def test_one_tick_above_upper_boundary_is_a_breakout():
    strategy = BreakoutStrategy(SMALL_PARAMS)
    signal = strategy.generate_signal(_bars_from_closes([100, 101, 99, 102, 100, Decimal("102.01")]))

    assert signal.action == SignalAction.BUY


# --- exit (lower) channel -----------------------------------------------------


def test_breakdown_below_exit_channel_yields_sell():
    # exit_period=3: prior closes 100,99,101 -> lower channel = 99
    strategy = BreakoutStrategy(SMALL_PARAMS)
    signal = strategy.generate_signal(_bars_from_closes([105, 104, 100, 99, 101, 98]))

    assert signal.action == SignalAction.SELL
    assert signal.price == Decimal("98")


def test_exactly_at_lower_boundary_is_not_a_breakdown():
    strategy = BreakoutStrategy(SMALL_PARAMS)
    signal = strategy.generate_signal(_bars_from_closes([105, 104, 100, 99, 101, 99]))

    assert signal.action == SignalAction.WAIT


def test_one_tick_below_lower_boundary_is_a_breakdown():
    strategy = BreakoutStrategy(SMALL_PARAMS)
    signal = strategy.generate_signal(_bars_from_closes([105, 104, 100, 99, 101, Decimal("98.99")]))

    assert signal.action == SignalAction.SELL


# --- warm-up -----------------------------------------------------------------


def test_insufficient_history_yields_wait_without_erroring():
    """entry_period=5 needs entry_period+1=6 total closes - exactly 5
    (one short) must still be WAIT, not an off-by-one crash."""
    strategy = BreakoutStrategy(SMALL_PARAMS)
    signal = strategy.generate_signal(_bars_from_closes([100, 101, 99, 102, 100]))

    assert signal.action == SignalAction.WAIT


def test_no_bars_yields_wait_with_no_price():
    strategy = BreakoutStrategy(SMALL_PARAMS)
    signal = strategy.generate_signal([])

    assert signal.action == SignalAction.WAIT
    assert signal.price is None


# --- params --------------------------------------------------------------------


def test_default_params_are_the_documented_turtle_system_1_defaults():
    strategy = BreakoutStrategy()

    assert strategy.params == {"entry_period": 20, "exit_period": 10}


def test_custom_params_override_defaults_and_are_used_not_hardcoded():
    """A strategy configured with a much shorter entry_period should reach
    a real signal on data too short for the default period=20, proving
    self.params is actually read rather than a hardcoded 20/10."""
    default_strategy = BreakoutStrategy()  # entry_period=20
    custom_strategy = BreakoutStrategy({"entry_period": 3, "exit_period": 2})
    bars = _bars_from_closes([100, 101, 99, 103])  # 4 bars: enough for period=3, not for period=20

    assert default_strategy.generate_signal(bars).action == SignalAction.WAIT
    assert custom_strategy.generate_signal(bars).action == SignalAction.BUY  # 103 > max(100,101,99)=101
    assert custom_strategy.params["entry_period"] == 3
    assert custom_strategy.params["exit_period"] == 2


def test_generate_signal_never_touches_broker_or_portfolio():
    """Strategy has no submit_order/buy/sell method at all - it's a pure
    function by construction, not just by convention."""
    strategy = BreakoutStrategy(SMALL_PARAMS)
    assert not hasattr(strategy, "submit_order")
    assert not hasattr(strategy, "buy")
    assert not hasattr(strategy, "sell")


# --- no pyramiding (backtest engine's job, not the strategy's) -----------------


def test_backtest_does_not_pyramid_on_consecutive_buy_signals():
    """A steadily rising series keeps closing above its own rolling
    entry channel for several bars in a row, so generate_signal() emits
    BUY on each of those bars independently (it's stateless - it has no
    idea a position is already open). The backtest engine's existing
    all-in/all-out execution (the same logic TrendStrategy already relies
    on) must still open exactly one position, not one per BUY signal.
    """
    strategy = BreakoutStrategy({"entry_period": 3, "exit_period": 2})
    bars = _bars_from_closes([100, 99, 101, 105, 110, 115, 120])

    result = run_backtest(strategy, bars, initial_capital=Decimal("10000"), symbol="TEST")

    assert result.num_trades == 1
