from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

from app.database.models import SignalAction
from app.market.provider import Bar
from app.strategies.trend import TrendStrategy

SMALL_PARAMS = {
    "ema_fast_period": 5,
    "ema_slow_period": 10,
    "rsi_period": 5,
    "rsi_overbought": 70,
}

# A noisy-but-clear uptrend: net drift up with small pullbacks so RSI stays
# under the overbought gate (a perfectly monotonic rise saturates RSI at 100
# and would otherwise force a WAIT despite the trend being obvious).
UPTREND_CLOSES = [100, 101, 99, 102, 100, 103, 101, 104, 102, 105, 103, 106, 104, 107, 105, 108, 106, 109]
DOWNTREND_CLOSES = [109, 108, 110, 107, 109, 106, 108, 105, 107, 104, 106, 103, 105, 102, 104, 101, 103, 100]
FLAT_CLOSES = [100] * 18


def _bars_from_closes(closes):
    ts = datetime(2024, 1, 1, tzinfo=timezone.utc)
    bars = []
    for i, close in enumerate(closes):
        price = Decimal(str(close))
        bars.append(
            Bar(
                timestamp=ts + timedelta(days=i),
                open=price,
                high=price,
                low=price,
                close=price,
                volume=1000,
            )
        )
    return bars


def test_clear_uptrend_yields_buy():
    strategy = TrendStrategy(SMALL_PARAMS)
    signal = strategy.generate_signal(_bars_from_closes(UPTREND_CLOSES))

    assert signal.action == SignalAction.BUY
    assert signal.price == Decimal("109")


def test_clear_downtrend_yields_sell():
    strategy = TrendStrategy(SMALL_PARAMS)
    signal = strategy.generate_signal(_bars_from_closes(DOWNTREND_CLOSES))

    assert signal.action == SignalAction.SELL
    assert signal.price == Decimal("100")


def test_flat_price_yields_wait():
    strategy = TrendStrategy(SMALL_PARAMS)
    signal = strategy.generate_signal(_bars_from_closes(FLAT_CLOSES))

    assert signal.action == SignalAction.WAIT
    assert signal.price == Decimal("100")


def test_insufficient_history_yields_wait_without_erroring():
    strategy = TrendStrategy(SMALL_PARAMS)  # ema_slow_period=10
    signal = strategy.generate_signal(_bars_from_closes(UPTREND_CLOSES[:5]))

    assert signal.action == SignalAction.WAIT


def test_no_bars_yields_wait_with_no_price():
    strategy = TrendStrategy(SMALL_PARAMS)
    signal = strategy.generate_signal([])

    assert signal.action == SignalAction.WAIT
    assert signal.price is None


def test_default_params_are_the_documented_trend_following_defaults():
    strategy = TrendStrategy()

    assert strategy.params == {
        "ema_fast_period": 50,
        "ema_slow_period": 200,
        "rsi_period": 14,
        "rsi_overbought": 70,
    }


def test_custom_params_override_defaults_and_are_used_not_hardcoded():
    """A stricter overbought threshold should turn what was a BUY into a WAIT,
    proving the strategy actually reads self.params rather than a constant."""
    lenient = TrendStrategy(SMALL_PARAMS)
    strict_params = dict(SMALL_PARAMS, rsi_overbought=10)
    strict = TrendStrategy(strict_params)

    bars = _bars_from_closes(UPTREND_CLOSES)

    assert lenient.generate_signal(bars).action == SignalAction.BUY
    assert strict.generate_signal(bars).action == SignalAction.WAIT
    assert strict.params["rsi_overbought"] == 10


def test_generate_signal_never_touches_broker_or_portfolio():
    """Strategy has no submit_order/buy/sell method at all - it's a pure
    function by construction, not just by convention."""
    strategy = TrendStrategy(SMALL_PARAMS)
    assert not hasattr(strategy, "submit_order")
    assert not hasattr(strategy, "buy")
    assert not hasattr(strategy, "sell")
