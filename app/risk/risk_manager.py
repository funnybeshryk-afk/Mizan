from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_DOWN, Decimal
from numbers import Real

from app.database.models import SignalAction
from app.risk.risk_profile import RiskProfile

# Fractional-share sizing precision - matches the app's Numeric(18,6) money
# columns. Rounding DOWN (never up) so a sized position never costs more
# than max_position_pct actually allows.
_QUANTITY_STEP = Decimal("0.000001")


def _to_decimal(value: Decimal | Real | str) -> Decimal:
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


@dataclass(frozen=True)
class PortfolioState:
    """A snapshot of just the numbers RiskManager needs to make a decision.
    Not a DB table, not the real Portfolio - the caller builds one from
    whatever bookkeeping it's doing (live trading or a backtest loop).
    """

    cash: Decimal
    position_qty: Decimal
    equity_peak: Decimal
    current_equity: Decimal
    day_start_equity: Decimal
    halted: bool = False


@dataclass(frozen=True)
class RiskDecision:
    """RiskManager's verdict on one proposed action.

    halt_for_day / halt_permanently are advisory signals for the caller to
    persist in its own state (RiskManager itself is stateless and re-derives
    everything from the PortfolioState it's handed each call) - the daily
    one should be cleared at the next day boundary, the permanent one only
    by a manual reset.
    """

    approved: bool
    reason: str | None = None
    quantity: Decimal | None = None
    stop_loss_price: Decimal | None = None
    take_profit_price: Decimal | None = None
    halt_for_day: bool = False
    halt_permanently: bool = False


class RiskManager:
    """Turns a strategy's BUY/SELL opinion into a sized, limit-checked
    decision under a RiskProfile. Purely a function of the arguments it's
    given each call - it holds no state of its own between evaluate() calls.
    """

    def evaluate(
        self,
        action: SignalAction,
        price: Decimal | Real,
        portfolio_state: PortfolioState,
        risk_profile: RiskProfile,
    ) -> RiskDecision:
        price = _to_decimal(price)

        if action == SignalAction.SELL:
            # Closing an existing position is always allowed, halted or not -
            # risk rules exist to control new exposure, not to trap the user
            # in a trade they're trying to get out of.
            return RiskDecision(approved=True)

        if action == SignalAction.WAIT:
            return RiskDecision(approved=False, reason="No action requested")

        # action == BUY from here on. Circuit breakers are checked before
        # sizing: if trading is halted for any reason, the size never matters.
        if portfolio_state.halted:
            return RiskDecision(approved=False, reason="Trading is halted")

        if portfolio_state.day_start_equity > 0:
            daily_loss_pct = (
                portfolio_state.day_start_equity - portfolio_state.current_equity
            ) / portfolio_state.day_start_equity
            if daily_loss_pct >= Decimal(str(risk_profile.max_daily_loss_pct)):
                return RiskDecision(
                    approved=False,
                    reason="Daily loss limit breached - halting new entries for the rest of the day",
                    halt_for_day=True,
                )

        if portfolio_state.equity_peak > 0:
            drawdown_pct = (
                portfolio_state.equity_peak - portfolio_state.current_equity
            ) / portfolio_state.equity_peak
            if drawdown_pct >= Decimal(str(risk_profile.max_drawdown_pct)):
                return RiskDecision(
                    approved=False,
                    reason="Max drawdown circuit breaker tripped - halted until manually reset",
                    halt_permanently=True,
                )

        raw_quantity = (
            portfolio_state.current_equity * Decimal(str(risk_profile.max_position_pct)) / price
        )
        quantity = raw_quantity.quantize(_QUANTITY_STEP, rounding=ROUND_DOWN)
        if quantity <= 0:
            return RiskDecision(
                approved=False,
                reason=(
                    "Capital too small for this price at the configured "
                    "max_position_pct - even a fractional share doesn't fit"
                ),
            )

        stop_loss_price = None
        take_profit_price = None
        if risk_profile.stop_loss_required:
            stop_loss_price = price * (Decimal("1") - Decimal(str(risk_profile.stop_loss_pct)))
            take_profit_price = price * (Decimal("1") + Decimal(str(risk_profile.take_profit_pct)))

        return RiskDecision(
            approved=True,
            quantity=quantity,
            stop_loss_price=stop_loss_price,
            take_profit_price=take_profit_price,
        )
