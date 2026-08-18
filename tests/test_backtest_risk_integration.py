from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

from app.backtesting.engine import run_backtest
from app.database.models import SignalAction
from app.market.provider import Bar
from app.risk.risk_profile import CONSERVATIVE, RiskProfile
from app.strategies.strategy import Signal, Strategy

# Reuse the exact stage-5 known-result fixture to prove risk_profile=None is
# still a byte-for-byte regression of that behaviour.
from tests.test_backtest_engine import KNOWN_RESULT_ACTIONS, KNOWN_RESULT_BARS


class FixedSequenceStrategy(Strategy):
    def __init__(self, actions):
        super().__init__({})
        self._actions = actions
        self._i = 0

    def generate_signal(self, bars):
        action = self._actions[self._i] if self._i < len(self._actions) else SignalAction.WAIT
        self._i += 1
        return Signal(action=action, price=bars[-1].close if bars else None)


def _bar(open_, high_, low_, close_, timestamp):
    o, h, l, c = (Decimal(str(v)) for v in (open_, high_, low_, close_))
    return Bar(timestamp=timestamp, open=o, high=h, low=l, close=c, volume=1000)


# --- risk_profile=None is unchanged from stage 5 ----------------------------


def test_risk_profile_none_is_a_regression_of_the_stage5_known_result():
    strategy = FixedSequenceStrategy(KNOWN_RESULT_ACTIONS)
    result = run_backtest(
        strategy, KNOWN_RESULT_BARS, initial_capital=Decimal("10000"), symbol="X", risk_profile=None
    )

    assert result.num_trades == 2
    assert result.closed_trade_pnls == [Decimal("-1000.000000000000")]
    assert result.final_equity == Decimal("9000.000000")


# --- sizing: CONSERVATIVE buys less than all-in -----------------------------


def test_conservative_position_is_smaller_than_all_in():
    ts = datetime(2024, 1, 1, tzinfo=timezone.utc)
    # BUY fills at bar1's open=100; a modest rally to bar2's close=104 (kept
    # under CONSERVATIVE's 5% take-profit at 105, so the position is still
    # open and marked-to-market, not force-closed) exposes how much was
    # actually put to work.
    bars = [_bar(100, 100, 100, 100, ts), _bar(100, 100, 100, 100, ts), _bar(100, 104, 100, 104, ts)]

    all_in_result = run_backtest(
        FixedSequenceStrategy([SignalAction.BUY]), bars, initial_capital=Decimal("100000"), symbol="X"
    )
    conservative_result = run_backtest(
        FixedSequenceStrategy([SignalAction.BUY]),
        bars,
        initial_capital=Decimal("100000"),
        symbol="X",
        risk_profile=CONSERVATIVE,
    )

    # All-in: 1000 shares at 100, worth 104 each after the rally -> 104000.
    assert all_in_result.equity_curve[-1].equity == Decimal("104000.000000")
    # CONSERVATIVE: 5% of 100000 = 50 shares -> only a fraction of the gain.
    assert conservative_result.equity_curve[-1].equity == Decimal("100200.000000")
    assert conservative_result.equity_curve[-1].equity < all_in_result.equity_curve[-1].equity


# --- stop-loss takes priority over the strategy's own SELL signal ----------


def test_conservative_stop_loss_closes_before_the_strategys_own_sell_signal():
    ts = datetime(2024, 1, 1, tzinfo=timezone.utc)
    bars = [
        _bar(100, 100, 100, 100, ts),
        _bar(100, 105, 99, 100, ts + timedelta(days=1)),  # entry bar (fill at open=100)
        _bar(95, 96, 90, 93, ts + timedelta(days=2)),  # low=90 breaches stop=97.5
        _bar(93, 93, 93, 93, ts + timedelta(days=3)),  # strategy's own SELL would fire here
    ]
    # Strategy would only say SELL on bar 3 - the stop must close it on bar 2.
    strategy = FixedSequenceStrategy(
        [SignalAction.BUY, SignalAction.WAIT, SignalAction.WAIT, SignalAction.SELL]
    )

    result = run_backtest(
        strategy, bars, initial_capital=Decimal("100000"), symbol="X", risk_profile=CONSERVATIVE
    )

    # Entry (50 shares @ 100) + forced stop-close @ 97.5 = 2 trades, done by bar 2.
    assert result.num_trades == 2
    assert result.closed_trade_pnls == [Decimal("-125.000000000000")]
    # The bar-3 SELL signal found no position left - a no-op, not a 3rd trade.
    assert result.equity_curve[-1].equity == Decimal("99875.000000000")


def test_stop_loss_does_not_trigger_on_the_bar_it_was_entered_on():
    ts = datetime(2024, 1, 1, tzinfo=timezone.utc)
    bars = [
        _bar(100, 100, 100, 100, ts),
        # Entry bar's low (50) is far below the 97.5 stop - must NOT trigger same-bar.
        _bar(100, 105, 50, 100, ts + timedelta(days=1)),
        _bar(98, 99, 98, 98, ts + timedelta(days=2)),  # safely above the stop
    ]
    strategy = FixedSequenceStrategy([SignalAction.BUY, SignalAction.WAIT, SignalAction.WAIT])

    result = run_backtest(
        strategy, bars, initial_capital=Decimal("100000"), symbol="X", risk_profile=CONSERVATIVE
    )

    assert result.num_trades == 1
    assert result.closed_trade_pnls == []


# --- daily-loss halt: blocks same-day retries, resets the next day ---------


def test_daily_loss_halt_blocks_a_same_day_retry_but_resets_the_next_day():
    day1 = datetime(2024, 1, 1, tzinfo=timezone.utc)
    day2 = datetime(2024, 1, 2, tzinfo=timezone.utc)
    bars = [
        _bar(100, 100, 100, 100, day1),
        _bar(100, 101, 99, 96, day1),  # entry fills here at open=100
        _bar(96, 97, 90, 91, day1),  # stop breached -> forced close, -2.5% of equity
        _bar(95, 96, 94, 95, day1),  # retry BUY attempt - same day, must be rejected
        _bar(98, 99, 97, 98, day2),  # next day - a fresh BUY must be allowed again
    ]
    actions = [
        SignalAction.BUY,
        SignalAction.WAIT,
        SignalAction.BUY,  # decided on the stop-close bar, retried on the very next bar (still day1)
        SignalAction.BUY,  # decided here, filled on day2
        SignalAction.WAIT,
    ]
    # 50% sizing + 5% stop -> one stopped trade alone can breach a 1% daily cap.
    test_profile = RiskProfile(
        name="TEST",
        max_position_pct=0.5,
        max_daily_loss_pct=0.01,
        max_drawdown_pct=0.5,
        stop_loss_required=True,
        stop_loss_pct=0.05,
        take_profit_pct=0.5,
        max_open_positions=5,
        max_leverage=1.0,
        capital_allocation_pct=1.0,
    )

    result = run_backtest(
        FixedSequenceStrategy(actions),
        bars,
        initial_capital=Decimal("100000"),
        symbol="X",
        risk_profile=test_profile,
    )

    # entry + stop-close + the day-2 entry = 3; the same-day retry is rejected (no trade).
    assert result.num_trades == 3
    assert result.closed_trade_pnls == [Decimal("-2500.000000000000")]


def test_drawdown_halt_persists_across_the_day_boundary():
    day1 = datetime(2024, 1, 1, tzinfo=timezone.utc)
    day2 = datetime(2024, 1, 2, tzinfo=timezone.utc)
    bars = [
        _bar(100, 100, 100, 100, day1),
        _bar(100, 101, 99, 96, day1),
        _bar(96, 97, 90, 91, day1),  # stop breached -> -2.5% equity, trips drawdown too
        _bar(95, 96, 94, 95, day1),  # same-day retry - rejected
        _bar(98, 99, 97, 98, day2),  # next day - still rejected, unlike a daily-only halt
    ]
    actions = [
        SignalAction.BUY,
        SignalAction.WAIT,
        SignalAction.BUY,
        SignalAction.BUY,
        SignalAction.WAIT,
    ]
    # Daily-loss threshold effectively disabled so only drawdown can trigger.
    dd_profile = RiskProfile(
        name="DDTEST",
        max_position_pct=0.5,
        max_daily_loss_pct=0.9,
        max_drawdown_pct=0.02,
        stop_loss_required=True,
        stop_loss_pct=0.05,
        take_profit_pct=0.5,
        max_open_positions=5,
        max_leverage=1.0,
        capital_allocation_pct=1.0,
    )

    result = run_backtest(
        FixedSequenceStrategy(actions),
        bars,
        initial_capital=Decimal("100000"),
        symbol="X",
        risk_profile=dd_profile,
    )

    # Only entry + stop-close ever execute - both the day-1 retry and the
    # day-2 attempt are rejected because the halt never auto-clears.
    assert result.num_trades == 2
    assert result.closed_trade_pnls == [Decimal("-2500.000000000000")]
