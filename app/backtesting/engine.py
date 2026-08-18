from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import ROUND_DOWN, Decimal
from numbers import Real

from app.broker.paper_broker import PaperBroker
from app.database.models import Instrument, SignalAction, TradeSide
from app.database.session import get_engine, get_session_factory
from app.market.provider import Bar
from app.portfolio.portfolio import Portfolio
from app.risk.risk_manager import PortfolioState, RiskManager
from app.risk.risk_profile import RiskProfile
from app.strategies.strategy import Strategy

_QUANTITY_STEP = Decimal("0.000001")


def _to_decimal(value: Decimal | Real | str) -> Decimal:
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


@dataclass(frozen=True)
class EquityPoint:
    timestamp: datetime
    equity: Decimal


@dataclass
class BacktestResult:
    initial_capital: Decimal
    final_equity: Decimal
    equity_curve: list[EquityPoint] = field(default_factory=list)
    num_trades: int = 0
    closed_trade_pnls: list[Decimal] = field(default_factory=list)


@dataclass
class _ExecutionOutcome:
    executed: bool = False
    closed_pnl: Decimal | None = None
    entry_stop_loss_price: Decimal | None = None
    entry_take_profit_price: Decimal | None = None
    halt_for_day: bool = False
    halt_permanently: bool = False


def _execute_all_in(
    portfolio: Portfolio,
    broker: PaperBroker,
    instrument: Instrument,
    action: SignalAction,
    price: Decimal,
) -> _ExecutionOutcome:
    """No-risk-profile sizing (stage 5 behaviour, unchanged): BUY goes
    all-in with free cash when flat, SELL exits the entire position when
    long. A BUY while already long, or a SELL while flat, is a no-op.
    """
    position = portfolio.get_position(instrument.symbol)
    current_qty = position.quantity if position is not None else Decimal("0")

    if action == SignalAction.BUY and current_qty == 0:
        quantity = (portfolio.cash / price).quantize(_QUANTITY_STEP, rounding=ROUND_DOWN)
        if quantity <= 0:
            return _ExecutionOutcome()
        broker.submit_order(portfolio.account, instrument, TradeSide.BUY, quantity, price=price)
        return _ExecutionOutcome(executed=True)

    if action == SignalAction.SELL and current_qty > 0:
        entry_price = position.avg_price
        broker.submit_order(portfolio.account, instrument, TradeSide.SELL, current_qty, price=price)
        closed_pnl = (price - entry_price) * current_qty
        return _ExecutionOutcome(executed=True, closed_pnl=closed_pnl)

    return _ExecutionOutcome()


def _execute_risk_managed(
    portfolio: Portfolio,
    broker: PaperBroker,
    instrument: Instrument,
    action: SignalAction,
    price: Decimal,
    risk_manager: RiskManager,
    risk_profile: RiskProfile,
    portfolio_state: PortfolioState,
) -> _ExecutionOutcome:
    """Same entry points as _execute_all_in, but BUY sizing/approval goes
    through RiskManager.evaluate() instead of spending all free cash. SELL
    still always closes the whole position - closing is never risk-gated.
    """
    position = portfolio.get_position(instrument.symbol)
    current_qty = position.quantity if position is not None else Decimal("0")

    if action == SignalAction.SELL and current_qty > 0:
        entry_price = position.avg_price
        broker.submit_order(portfolio.account, instrument, TradeSide.SELL, current_qty, price=price)
        closed_pnl = (price - entry_price) * current_qty
        return _ExecutionOutcome(executed=True, closed_pnl=closed_pnl)

    if action == SignalAction.BUY and current_qty == 0:
        decision = risk_manager.evaluate(SignalAction.BUY, price, portfolio_state, risk_profile)
        outcome = _ExecutionOutcome(
            halt_for_day=decision.halt_for_day, halt_permanently=decision.halt_permanently
        )
        if not decision.approved or not decision.quantity:
            return outcome
        broker.submit_order(
            portfolio.account, instrument, TradeSide.BUY, decision.quantity, price=price
        )
        outcome.executed = True
        outcome.entry_stop_loss_price = decision.stop_loss_price
        outcome.entry_take_profit_price = decision.take_profit_price
        return outcome

    return _ExecutionOutcome()


def _force_close(
    portfolio: Portfolio, broker: PaperBroker, instrument: Instrument, trigger_price: Decimal
) -> Decimal:
    """Unconditionally closes the current position at trigger_price (a
    stop-loss or take-profit breach) - not a strategy decision, so it
    doesn't go through RiskManager or the normal pending-order path."""
    position = portfolio.get_position(instrument.symbol)
    current_qty = position.quantity
    entry_price = position.avg_price
    broker.submit_order(portfolio.account, instrument, TradeSide.SELL, current_qty, price=trigger_price)
    return (trigger_price - entry_price) * current_qty


def run_backtest(
    strategy: Strategy,
    bars: list[Bar],
    initial_capital: Decimal | Real = Decimal("10000"),
    symbol: str = "BACKTEST",
    risk_profile: RiskProfile | None = None,
) -> BacktestResult:
    """Replays a strategy over historical bars on a fresh, isolated
    in-memory Portfolio/PaperBroker - never the live database.

    No-lookahead by construction: the decision for bar i is generated from
    bars[0:i+1] only, and any resulting order is not filled until bar i+1's
    open (recorded as `pending`, applied at the *start* of the next loop
    iteration, before that day's own decision or equity mark). If bar i is
    the last bar, a pending order from it is simply never filled - the
    history ended first.

    With risk_profile=None, behaves exactly as before (stage 5): all-in/
    all-out sizing, no stop-loss/take-profit, no loss limits. Passing a
    RiskProfile switches entries to RiskManager-sized quantities, tracks a
    per-entry stop-loss/take-profit that's checked against every
    subsequent bar's low/high (a breach force-closes the position, taking
    priority over the strategy's own signal that bar), and enforces the
    profile's daily-loss and drawdown circuit breakers.
    """
    initial_capital = _to_decimal(initial_capital)
    risk_manager = RiskManager() if risk_profile is not None else None

    engine = get_engine("sqlite:///:memory:")
    session = get_session_factory(engine)()
    try:
        portfolio = Portfolio.create(session, initial_cash=initial_capital)
        broker = PaperBroker(portfolio, market_provider=None)
        instrument = portfolio.get_or_create_instrument(symbol)

        equity_curve: list[EquityPoint] = []
        closed_trade_pnls: list[Decimal] = []
        num_trades = 0
        pending_action: SignalAction | None = None

        last_equity = initial_capital
        equity_peak = initial_capital
        day_start_equity = initial_capital
        current_day = None
        halted_for_today = False
        halted_permanently = False
        stop_loss_price: Decimal | None = None
        take_profit_price: Decimal | None = None

        for i, bar in enumerate(bars):
            bar_day = bar.timestamp.date()
            if current_day is None:
                current_day = bar_day
            elif bar_day != current_day:
                day_start_equity = last_equity
                halted_for_today = False
                current_day = bar_day

            # Stop-loss/take-profit check on an already-open position, using
            # levels set on a *prior* bar - so a fresh entry this bar is
            # never checked against the same bar it was filled on. This
            # takes priority over the strategy's own signal for this bar.
            if risk_profile is not None and stop_loss_price is not None:
                if bar.low <= stop_loss_price:
                    closed_trade_pnls.append(_force_close(portfolio, broker, instrument, stop_loss_price))
                    num_trades += 1
                    stop_loss_price = None
                    take_profit_price = None
                elif take_profit_price is not None and bar.high >= take_profit_price:
                    closed_trade_pnls.append(
                        _force_close(portfolio, broker, instrument, take_profit_price)
                    )
                    num_trades += 1
                    stop_loss_price = None
                    take_profit_price = None

            if pending_action is not None:
                if risk_profile is not None:
                    position = portfolio.get_position(symbol)
                    portfolio_state = PortfolioState(
                        cash=portfolio.cash,
                        position_qty=position.quantity if position is not None else Decimal("0"),
                        equity_peak=equity_peak,
                        current_equity=last_equity,
                        day_start_equity=day_start_equity,
                        halted=halted_permanently or halted_for_today,
                    )
                    outcome = _execute_risk_managed(
                        portfolio,
                        broker,
                        instrument,
                        pending_action,
                        bar.open,
                        risk_manager,
                        risk_profile,
                        portfolio_state,
                    )
                    halted_for_today = halted_for_today or outcome.halt_for_day
                    halted_permanently = halted_permanently or outcome.halt_permanently
                else:
                    outcome = _execute_all_in(portfolio, broker, instrument, pending_action, bar.open)

                if outcome.executed:
                    num_trades += 1
                    if outcome.closed_pnl is not None:
                        closed_trade_pnls.append(outcome.closed_pnl)
                        stop_loss_price = None
                        take_profit_price = None
                    else:
                        stop_loss_price = outcome.entry_stop_loss_price
                        take_profit_price = outcome.entry_take_profit_price
                pending_action = None

            signal = strategy.generate_signal(bars[: i + 1])
            if signal.action in (SignalAction.BUY, SignalAction.SELL):
                pending_action = signal.action

            position = portfolio.get_position(symbol)
            position_qty = position.quantity if position is not None else Decimal("0")
            equity = portfolio.cash + position_qty * bar.close
            equity_curve.append(EquityPoint(timestamp=bar.timestamp, equity=equity))
            last_equity = equity
            if equity > equity_peak:
                equity_peak = equity

        final_equity = equity_curve[-1].equity if equity_curve else initial_capital

        return BacktestResult(
            initial_capital=initial_capital,
            final_equity=final_equity,
            equity_curve=equity_curve,
            num_trades=num_trades,
            closed_trade_pnls=closed_trade_pnls,
        )
    finally:
        session.close()
