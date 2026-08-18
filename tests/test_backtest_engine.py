from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

from app.backtesting.engine import run_backtest
from app.database.models import SignalAction
from app.database.session import DB_PATH
from app.market.provider import Bar
from app.strategies.strategy import Signal, Strategy
from app.strategies.trend import TrendStrategy


def _bar(open_, close_, day, base=None):
    base = base or datetime(2024, 1, 1, tzinfo=timezone.utc)
    o, c = Decimal(str(open_)), Decimal(str(close_))
    return Bar(
        timestamp=base + timedelta(days=day),
        open=o,
        high=max(o, c),
        low=min(o, c),
        close=c,
        volume=1000,
    )


def _bars_from_closes(closes, base=None):
    return [_bar(c, c, i, base) for i, c in enumerate(closes)]


class FixedSequenceStrategy(Strategy):
    """Test double: emits one scripted action per call, in order, then WAIT
    forever. Lets the engine-mechanics tests (fill timing, sizing, equity
    calc) use exactly known decisions instead of depending on indicator
    math, which is already covered in test_trend_strategy.py."""

    def __init__(self, actions: list[SignalAction]):
        super().__init__({})
        self._actions = actions
        self._call_count = 0

    def generate_signal(self, bars: list[Bar]) -> Signal:
        action = (
            self._actions[self._call_count]
            if self._call_count < len(self._actions)
            else SignalAction.WAIT
        )
        self._call_count += 1
        price = bars[-1].close if bars else None
        return Signal(action=action, price=price)


# --- Hand-computed known result ---------------------------------------------
#
# 5 bars, scripted actions [BUY, WAIT, SELL, WAIT, WAIT] (one per bar/call):
#   i=0: no pending order yet. Decide BUY -> pending for bar1's open.
#        equity0 = cash(10000) + 0*close0(100) = 10000
#   i=1: fill BUY at bar1.open=100 -> qty = 10000/100 = 100 exactly, cash=0.
#        Decide WAIT.
#        equity1 = 0 + 100*close1(110) = 11000
#   i=2: no pending order. Decide SELL -> pending for bar3's open.
#        equity2 = 0 + 100*close2(120) = 12000
#   i=3: fill SELL at bar3.open=90 -> proceeds=9000, cash=9000, qty=0.
#        closed trade pnl = (90-100)*100 = -1000 (a loss)
#        Decide WAIT.
#        equity3 = 9000 + 0*close3(85) = 9000
#   i=4: no pending order. Decide WAIT.
#        equity4 = 9000 + 0*close4(95) = 9000
#
# So: num_trades=2, one closed trade (a loss of -1000), final_equity=9000.

KNOWN_RESULT_BARS = [
    _bar(100, 100, 0),
    _bar(100, 110, 1),  # BUY fills here at open=100
    _bar(115, 120, 2),
    _bar(90, 85, 3),  # SELL fills here at open=90
    _bar(92, 95, 4),
]
KNOWN_RESULT_ACTIONS = [
    SignalAction.BUY,
    SignalAction.WAIT,
    SignalAction.SELL,
    SignalAction.WAIT,
    SignalAction.WAIT,
]


def test_matches_hand_computed_known_result():
    strategy = FixedSequenceStrategy(KNOWN_RESULT_ACTIONS)
    result = run_backtest(strategy, KNOWN_RESULT_BARS, initial_capital=Decimal("10000"), symbol="X")

    assert [p.equity for p in result.equity_curve] == [
        Decimal("10000"),
        Decimal("11000.000000"),
        Decimal("12000.000000"),
        Decimal("9000.000000"),
        Decimal("9000.000000"),
    ]
    assert result.num_trades == 2
    assert result.closed_trade_pnls == [Decimal("-1000.000000000000")]
    assert result.final_equity == Decimal("9000.000000")


def test_buy_fills_at_next_bars_open_not_the_signal_bars_close():
    # BUY decided at bar0 (close=100). If it wrongly filled at bar0's close
    # (100), 100 shares would be bought and equity1 = 100*110 = 11000. It
    # must instead fill at bar1's open (500): 20 shares, equity1 = 20*110 = 2200.
    bars = [_bar(100, 100, 0), _bar(500, 110, 1), _bar(500, 110, 2)]
    strategy = FixedSequenceStrategy([SignalAction.BUY])
    result = run_backtest(strategy, bars, initial_capital=Decimal("10000"), symbol="X")

    assert result.equity_curve[1].equity == Decimal("2200.000000")


def test_a_signal_on_the_last_bar_is_never_executed():
    bars = [_bar(100, 100, 0), _bar(100, 100, 1)]
    strategy = FixedSequenceStrategy([SignalAction.WAIT, SignalAction.BUY])
    result = run_backtest(strategy, bars, initial_capital=Decimal("10000"), symbol="X")

    assert result.num_trades == 0
    assert result.final_equity == Decimal("10000")


def test_buy_while_already_long_is_a_no_op_not_a_pyramid():
    bars = [_bar(100, 100, 0), _bar(100, 100, 1), _bar(100, 100, 2), _bar(100, 100, 3)]
    strategy = FixedSequenceStrategy([SignalAction.BUY, SignalAction.BUY, SignalAction.WAIT])
    result = run_backtest(strategy, bars, initial_capital=Decimal("10000"), symbol="X")

    assert result.num_trades == 1


def test_sell_while_flat_is_a_no_op():
    bars = [_bar(100, 100, 0), _bar(100, 100, 1)]
    strategy = FixedSequenceStrategy([SignalAction.SELL, SignalAction.WAIT])
    result = run_backtest(strategy, bars, initial_capital=Decimal("10000"), symbol="X")

    assert result.num_trades == 0


def test_run_backtest_never_touches_the_live_database():
    existed_before = DB_PATH.exists()
    mtime_before = DB_PATH.stat().st_mtime if existed_before else None

    strategy = FixedSequenceStrategy([SignalAction.BUY, SignalAction.SELL])
    run_backtest(strategy, KNOWN_RESULT_BARS, initial_capital=Decimal("1000"), symbol="X")

    if existed_before:
        assert DB_PATH.stat().st_mtime == mtime_before
    else:
        assert not DB_PATH.exists()


# --- Anti-lookahead ----------------------------------------------------------


def test_result_is_unaffected_by_a_future_bar_that_has_not_happened_yet():
    """Two bar sequences share bars[0:10] exactly, then diverge sharply at
    index 10 (a mild next bar vs. an extreme price spike). If the engine
    leaked bar 10 into the decision made at i=9 (e.g. an off-by-one passing
    bars[:i+2] instead of bars[:i+1]), the spike would flip that decision -
    confirmed below by calling generate_signal directly with bar 10 included,
    which does change BUY into WAIT. Since run_backtest must not leak it, the
    two backtests must agree exactly through index 9 regardless.
    """
    params = {"ema_fast_period": 5, "ema_slow_period": 10, "rsi_period": 5, "rsi_overbought": 70}
    base_closes = [100, 101, 99, 102, 100, 103, 101, 104, 102, 105]

    # Sanity check this scenario actually has discriminating power: leaking
    # the spike into the decision changes it, so the test isn't vacuous.
    decision_blind = TrendStrategy(params).generate_signal(_bars_from_closes(base_closes))
    decision_if_leaked = TrendStrategy(params).generate_signal(
        _bars_from_closes(base_closes + [100000])
    )
    assert decision_blind.action == SignalAction.BUY
    assert decision_if_leaked.action == SignalAction.WAIT
    assert decision_blind.action != decision_if_leaked.action

    bars_mild = _bars_from_closes(base_closes + [106])
    bars_extreme = _bars_from_closes(base_closes + [100000])

    result_mild = run_backtest(TrendStrategy(params), bars_mild, initial_capital=Decimal("10000"), symbol="X")
    result_extreme = run_backtest(
        TrendStrategy(params), bars_extreme, initial_capital=Decimal("10000"), symbol="X"
    )

    equity_mild_through_9 = [p.equity for p in result_mild.equity_curve[:10]]
    equity_extreme_through_9 = [p.equity for p in result_extreme.equity_curve[:10]]
    assert equity_mild_through_9 == equity_extreme_through_9

    # The decision at i=9 (BUY, per decision_blind above) must be identical
    # between the two runs - both execute exactly one order by that point.
    assert result_mild.num_trades == result_extreme.num_trades == 1
