from __future__ import annotations

import json
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy.orm import Session

from app.database.models import Instrument
from app.database.models import Signal as SignalRecord
from app.database.models import SignalAction
from app.market.provider import Bar
from app.strategies.strategy import Signal, Strategy


def _persist_signal(
    session: Session,
    strategy_name: str,
    strategy_params_json: str,
    instrument: Instrument,
    action: SignalAction,
    price: Decimal | None,
) -> None:
    session.add(
        SignalRecord(
            strategy_name=strategy_name,
            strategy_params_json=strategy_params_json,
            instrument_id=instrument.id,
            action=action,
            generated_at=datetime.now(timezone.utc),
            price_at_signal=price,
        )
    )
    session.commit()


def evaluate_and_log(
    session: Session, strategy: Strategy, instrument: Instrument, bars: list[Bar]
) -> Signal:
    """Runs a Strategy against bars and persists the resulting Signal -
    including WAIT - to the database. The Strategy itself stays a pure
    function; this is the one place that bridges its output to storage.
    """
    signal = strategy.generate_signal(bars)
    _persist_signal(
        session,
        type(strategy).__name__,
        json.dumps(strategy.params, sort_keys=True),
        instrument,
        signal.action,
        signal.price,
    )
    return signal


def log_risk_signal(
    session: Session,
    instrument: Instrument,
    action: SignalAction,
    price: Decimal | None,
    reason: str = "",
) -> None:
    """Persists a risk-manager-forced action (a stop-loss/take-profit
    position exit) to the same Signal history as strategy-driven signals,
    reusing the same persistence path as evaluate_and_log() rather than a
    second one. `action` should be RISK_STOP_LOSS or RISK_TAKE_PROFIT -
    those values never come from Strategy.generate_signal(), so a row here
    is always distinguishable from a strategy-driven BUY/SELL/WAIT.
    """
    _persist_signal(
        session,
        "RiskManager",
        json.dumps({"reason": reason}, sort_keys=True),
        instrument,
        action,
        price,
    )
