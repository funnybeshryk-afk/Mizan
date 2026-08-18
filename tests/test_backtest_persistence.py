from __future__ import annotations

import json
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.backtesting.engine import run_backtest
from app.backtesting.runner import save_backtest_run
from app.database.models import Account, BacktestRun, Instrument
from app.database.models import SignalAction as Action
from app.database.session import get_engine, get_session_factory
from app.strategies.strategy import Signal, Strategy


class FixedSequenceStrategy(Strategy):
    def __init__(self, actions, params=None):
        super().__init__(params or {})
        self._actions = actions
        self._i = 0

    def generate_signal(self, bars):
        action = self._actions[self._i] if self._i < len(self._actions) else Action.WAIT
        self._i += 1
        return Signal(action=action, price=bars[-1].close if bars else None)


@pytest.fixture
def session():
    engine = get_engine("sqlite:///:memory:")
    session_factory = get_session_factory(engine)
    s = session_factory()
    yield s
    s.close()


@pytest.fixture
def instrument(session):
    inst = Instrument(symbol="AAPL", asset_class="US_EQUITY")
    session.add(inst)
    session.commit()
    return inst


def _run_known_result(session):
    from tests.test_backtest_engine import KNOWN_RESULT_ACTIONS, KNOWN_RESULT_BARS

    strategy = FixedSequenceStrategy(KNOWN_RESULT_ACTIONS, params={"lookback": 3})
    result = run_backtest(strategy, KNOWN_RESULT_BARS, initial_capital=Decimal("10000"), symbol="X")
    return strategy, result


def test_save_backtest_run_persists_all_fields(session, instrument):
    strategy, result = _run_known_result(session)

    run = save_backtest_run(
        session, strategy, instrument, date(2024, 1, 1), date(2024, 1, 5), result
    )

    assert run.id is not None
    assert run.strategy_name == "FixedSequenceStrategy"
    assert json.loads(run.strategy_params_json) == {"lookback": 3}
    assert run.instrument_id == instrument.id
    assert run.start_date == date(2024, 1, 1)
    assert run.end_date == date(2024, 1, 5)
    assert run.initial_capital == Decimal("10000")
    assert run.final_capital == Decimal("9000.000000")
    assert run.total_return_pct == Decimal("-10.0")
    assert run.max_drawdown_pct == Decimal("25.00")
    assert run.win_rate_pct == Decimal("0")
    assert run.num_trades == 2
    assert run.created_at is not None


def test_equity_curve_json_roundtrips(session, instrument):
    strategy, result = _run_known_result(session)

    run = save_backtest_run(
        session, strategy, instrument, date(2024, 1, 1), date(2024, 1, 5), result
    )

    stored = json.loads(run.equity_curve_json)
    assert len(stored) == len(result.equity_curve)
    assert [Decimal(point["equity"]) for point in stored] == [
        p.equity for p in result.equity_curve
    ]


def test_save_backtest_run_does_not_create_or_touch_any_account(session, instrument):
    strategy, result = _run_known_result(session)

    save_backtest_run(session, strategy, instrument, date(2024, 1, 1), date(2024, 1, 5), result)

    accounts = list(session.execute(select(Account)).scalars())
    assert accounts == []


def test_each_run_is_recorded_independently(session, instrument):
    strategy, result = _run_known_result(session)
    save_backtest_run(session, strategy, instrument, date(2024, 1, 1), date(2024, 1, 5), result)
    save_backtest_run(session, strategy, instrument, date(2024, 2, 1), date(2024, 2, 5), result)

    runs = list(session.execute(select(BacktestRun)).scalars())
    assert len(runs) == 2
