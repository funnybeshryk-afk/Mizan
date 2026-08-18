from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

from app.backtesting.engine import run_backtest
from app.database.models import SignalAction
from app.market.provider import Bar
from app.strategies.mean_reversion import MeanReversionStrategy

# period=5 window [96, 101, 101, 101, 101]: mean=100, population stddev=2
# (deviations -4,1,1,1,1 -> squares 16,1,1,1,1 -> variance=20/5=4 -> sqrt=2).
# With num_std=2: lower band=100-2*2=96, middle band=100.
SMALL_PARAMS = {"period": 5, "num_std": 2.0}
PRIOR_WINDOW = [96, 101, 101, 101, 101]


def _bars_from_closes(closes):
    ts = datetime(2024, 1, 1, tzinfo=timezone.utc)
    bars = []
    for i, close in enumerate(closes):
        price = Decimal(str(close))
        bars.append(
            Bar(timestamp=ts + timedelta(days=i), open=price, high=price, low=price, close=price, volume=1000)
        )
    return bars


# --- lower band (entry) --------------------------------------------------------


def test_breakdown_below_lower_band_yields_buy():
    strategy = MeanReversionStrategy(SMALL_PARAMS)
    signal = strategy.generate_signal(_bars_from_closes(PRIOR_WINDOW + [90]))

    assert signal.action == SignalAction.BUY
    assert signal.price == Decimal("90")


def test_exactly_at_lower_band_is_not_a_breakdown():
    """Current close equal to (not below) the lower band must not trigger -
    a strict breakdown, not a touch."""
    strategy = MeanReversionStrategy(SMALL_PARAMS)
    signal = strategy.generate_signal(_bars_from_closes(PRIOR_WINDOW + [96]))

    assert signal.action == SignalAction.WAIT


def test_one_tick_below_lower_band_is_a_breakdown():
    strategy = MeanReversionStrategy(SMALL_PARAMS)
    signal = strategy.generate_signal(_bars_from_closes(PRIOR_WINDOW + [Decimal("95.99")]))

    assert signal.action == SignalAction.BUY


# --- middle band (exit) --------------------------------------------------------


def test_return_above_middle_band_yields_sell():
    strategy = MeanReversionStrategy(SMALL_PARAMS)
    signal = strategy.generate_signal(_bars_from_closes(PRIOR_WINDOW + [101]))

    assert signal.action == SignalAction.SELL
    assert signal.price == Decimal("101")


def test_exactly_at_middle_band_is_not_an_exit_signal():
    strategy = MeanReversionStrategy(SMALL_PARAMS)
    signal = strategy.generate_signal(_bars_from_closes(PRIOR_WINDOW + [100]))

    assert signal.action == SignalAction.WAIT


def test_one_tick_above_middle_band_is_an_exit_signal():
    strategy = MeanReversionStrategy(SMALL_PARAMS)
    signal = strategy.generate_signal(_bars_from_closes(PRIOR_WINDOW + [Decimal("100.01")]))

    assert signal.action == SignalAction.SELL


# --- warm-up -----------------------------------------------------------------


def test_insufficient_history_yields_wait_without_erroring():
    """period=5 needs period+1=6 total closes - exactly 5 (one short) must
    still be WAIT, not an off-by-one crash."""
    strategy = MeanReversionStrategy(SMALL_PARAMS)
    signal = strategy.generate_signal(_bars_from_closes(PRIOR_WINDOW))

    assert signal.action == SignalAction.WAIT


def test_no_bars_yields_wait_with_no_price():
    strategy = MeanReversionStrategy(SMALL_PARAMS)
    signal = strategy.generate_signal([])

    assert signal.action == SignalAction.WAIT
    assert signal.price is None


# --- params --------------------------------------------------------------------


def test_default_params_are_the_documented_bollinger_defaults():
    strategy = MeanReversionStrategy()

    assert strategy.params == {"period": 20, "num_std": 2.0}


def test_custom_params_override_defaults_and_are_used_not_hardcoded():
    """A strategy configured with a much shorter period should reach a
    real signal on data too short for the default period=20, proving
    self.params is actually read rather than a hardcoded 20/2.0."""
    default_strategy = MeanReversionStrategy()  # period=20
    custom_strategy = MeanReversionStrategy({"period": 5, "num_std": 2.0})
    bars = _bars_from_closes(PRIOR_WINDOW + [90])  # 6 bars: enough for period=5, not period=20

    assert default_strategy.generate_signal(bars).action == SignalAction.WAIT
    assert custom_strategy.generate_signal(bars).action == SignalAction.BUY
    assert custom_strategy.params["period"] == 5
    assert custom_strategy.params["num_std"] == 2.0


def test_generate_signal_never_touches_broker_or_portfolio():
    """Strategy has no submit_order/buy/sell method at all - it's a pure
    function by construction, not just by convention."""
    strategy = MeanReversionStrategy(SMALL_PARAMS)
    assert not hasattr(strategy, "submit_order")
    assert not hasattr(strategy, "buy")
    assert not hasattr(strategy, "sell")


# --- no pyramiding (backtest engine's job, not the strategy's) -----------------


def test_backtest_does_not_pyramid_on_consecutive_buy_signals():
    """A steadily falling series keeps closing below its own (lagging)
    rolling lower band for several bars in a row, so generate_signal()
    emits BUY on each of those bars independently (it's stateless - it
    has no idea a position is already open). The backtest engine's
    existing all-in/all-out execution (the same logic every other
    strategy already relies on) must still open exactly one position.
    """
    strategy = MeanReversionStrategy({"period": 3, "num_std": 1.0})
    bars = _bars_from_closes([100, 100, 100, 90, 80, 70, 60])

    result = run_backtest(strategy, bars, initial_capital=Decimal("10000"), symbol="TEST")

    assert result.num_trades == 1


def test_backtest_reversion_round_trip_exits_on_return_to_mean():
    """A drop below the lower band followed by a recovery above the
    middle band should open then close exactly one round trip - proving
    the SELL exit condition actually fires through the backtest engine,
    not just in isolated generate_signal() calls."""
    strategy = MeanReversionStrategy(SMALL_PARAMS)
    # Prior window + a breakdown (BUY should fire and get filled), then a
    # climb back through and above the middle band (100) to exit.
    bars = _bars_from_closes(PRIOR_WINDOW + [90, 95, 99, 103, 105])

    result = run_backtest(strategy, bars, initial_capital=Decimal("10000"), symbol="TEST")

    assert result.num_trades == 2  # one entry, one exit
    assert len(result.closed_trade_pnls) == 1
