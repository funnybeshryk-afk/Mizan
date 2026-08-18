from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.database.models import Instrument
from app.database.models import Signal as SignalRecord
from app.database.models import SignalAction
from app.database.session import get_engine, get_session_factory
from app.market.provider import Bar
from app.portfolio.portfolio import Portfolio
from app.strategies.runner import evaluate_and_log
from app.strategies.trend import TrendStrategy

SMALL_PARAMS = {
    "ema_fast_period": 5,
    "ema_slow_period": 10,
    "rsi_period": 5,
    "rsi_overbought": 70,
}

UPTREND_CLOSES = [100, 101, 99, 102, 100, 103, 101, 104, 102, 105, 103, 106, 104, 107, 105, 108, 106, 109]
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


@pytest.fixture
def session():
    engine = get_engine("sqlite:///:memory:")
    session_factory = get_session_factory(engine)
    portfolio = Portfolio.create(session_factory(), initial_cash=10000)
    yield portfolio.session
    portfolio.session.close()


def _all_signals(session) -> list[SignalRecord]:
    return list(session.execute(select(SignalRecord)).scalars())


def test_evaluate_and_log_persists_a_buy_signal(session):
    instrument = Instrument(symbol="AAPL", asset_class="US_EQUITY")
    session.add(instrument)
    session.commit()

    strategy = TrendStrategy(SMALL_PARAMS)
    signal = evaluate_and_log(session, strategy, instrument, _bars_from_closes(UPTREND_CLOSES))

    assert signal.action == SignalAction.BUY

    records = _all_signals(session)
    assert len(records) == 1
    record = records[0]
    assert record.action == SignalAction.BUY
    assert record.instrument_id == instrument.id
    assert record.strategy_name == "TrendStrategy"
    assert record.price_at_signal == Decimal("109")
    assert record.generated_at is not None


def test_evaluate_and_log_persists_wait_signals_too(session):
    instrument = Instrument(symbol="MSFT", asset_class="US_EQUITY")
    session.add(instrument)
    session.commit()

    strategy = TrendStrategy(SMALL_PARAMS)
    signal = evaluate_and_log(session, strategy, instrument, _bars_from_closes(FLAT_CLOSES))

    assert signal.action == SignalAction.WAIT

    records = _all_signals(session)
    assert len(records) == 1
    assert records[0].action == SignalAction.WAIT


def test_strategy_params_are_recorded_verbatim_as_json(session):
    instrument = Instrument(symbol="AAPL", asset_class="US_EQUITY")
    session.add(instrument)
    session.commit()

    custom_params = dict(SMALL_PARAMS, rsi_overbought=55)
    strategy = TrendStrategy(custom_params)
    evaluate_and_log(session, strategy, instrument, _bars_from_closes(UPTREND_CLOSES))

    record = _all_signals(session)[0]
    stored_params = json.loads(record.strategy_params_json)
    assert stored_params == custom_params


def test_every_call_appends_a_new_row_even_for_the_same_instrument(session):
    instrument = Instrument(symbol="AAPL", asset_class="US_EQUITY")
    session.add(instrument)
    session.commit()

    strategy = TrendStrategy(SMALL_PARAMS)
    evaluate_and_log(session, strategy, instrument, _bars_from_closes(UPTREND_CLOSES))
    evaluate_and_log(session, strategy, instrument, _bars_from_closes(FLAT_CLOSES))

    records = _all_signals(session)
    assert len(records) == 2
    assert {r.action for r in records} == {SignalAction.BUY, SignalAction.WAIT}
