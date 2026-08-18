from __future__ import annotations

import json
from datetime import date

from sqlalchemy.orm import Session

from app.backtesting.engine import BacktestResult
from app.backtesting.metrics import max_drawdown_pct, total_return_pct, win_rate_pct
from app.database.models import BacktestRun, Instrument
from app.risk.risk_profile import RiskProfile
from app.strategies.strategy import Strategy


def save_backtest_run(
    session: Session,
    strategy: Strategy,
    instrument: Instrument,
    start_date: date,
    end_date: date,
    result: BacktestResult,
    risk_profile: RiskProfile | None = None,
) -> BacktestRun:
    """Persists a completed backtest run into the caller's own (real)
    session/database - never the isolated in-memory one run_backtest() uses
    internally - and, per BacktestRun's design, without any Account link.
    risk_profile is recorded by name only (a plain string, not a FK) since
    RiskProfile is a value object, not a database table.
    """
    equity_values = [point.equity for point in result.equity_curve]

    run = BacktestRun(
        strategy_name=type(strategy).__name__,
        strategy_params_json=json.dumps(strategy.params, sort_keys=True),
        instrument_id=instrument.id,
        start_date=start_date,
        end_date=end_date,
        initial_capital=result.initial_capital,
        final_capital=result.final_equity,
        total_return_pct=total_return_pct(result.initial_capital, result.final_equity),
        max_drawdown_pct=max_drawdown_pct(equity_values),
        win_rate_pct=win_rate_pct(result.closed_trade_pnls),
        num_trades=result.num_trades,
        equity_curve_json=json.dumps(
            [
                {"timestamp": point.timestamp.isoformat(), "equity": str(point.equity)}
                for point in result.equity_curve
            ]
        ),
        risk_profile_name=risk_profile.name if risk_profile is not None else None,
    )
    session.add(run)
    session.commit()
    return run
