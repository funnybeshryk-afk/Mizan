from __future__ import annotations

from dataclasses import replace
from decimal import ROUND_DOWN, Decimal

import pytest

from app.database.models import SignalAction
from app.risk.risk_manager import PortfolioState, RiskDecision, RiskManager
from app.risk.risk_profile import CONSERVATIVE

MANAGER = RiskManager()


def _state(**overrides) -> PortfolioState:
    defaults = dict(
        cash=Decimal("100000"),
        position_qty=Decimal("0"),
        equity_peak=Decimal("100000"),
        current_equity=Decimal("100000"),
        day_start_equity=Decimal("100000"),
        halted=False,
    )
    defaults.update(overrides)
    return PortfolioState(**defaults)


# --- sizing ------------------------------------------------------------------


def test_sizing_matches_hand_computed_numbers():
    # 100000 * 0.05 / 50 = 100 shares; stop = 50*0.975=48.75; take-profit = 50*1.05=52.50
    decision = MANAGER.evaluate(SignalAction.BUY, Decimal("50"), _state(), CONSERVATIVE)

    assert decision.approved is True
    assert decision.quantity == 100
    assert decision.stop_loss_price == Decimal("48.750")
    assert decision.take_profit_price == Decimal("52.50")


def test_sizing_gives_a_fractional_quantity_instead_of_silently_rounding_to_zero():
    """A price where the naive floor-to-integer sizing from before this fix
    would round to 0 and silently reject - now correctly sizes a fractional
    share instead, since the risk budget genuinely allows a small position."""
    # 100000 * 0.05 / 100000 = 0.05 shares - a real, tradeable fractional size.
    decision = MANAGER.evaluate(SignalAction.BUY, Decimal("100000"), _state(), CONSERVATIVE)

    assert decision.approved is True
    assert decision.quantity == Decimal("0.05")


def test_sizing_rejects_with_a_clear_reason_when_truly_too_small():
    # Even at 6-decimal precision, 100000 * 0.05 / price rounds to 0 here.
    decision = MANAGER.evaluate(SignalAction.BUY, Decimal("20000000000"), _state(), CONSERVATIVE)

    assert decision.approved is False
    assert decision.quantity is None
    assert "too small" in decision.reason.lower()


@pytest.mark.parametrize("capital", ["100", "1000", "10000", "1000000"])
@pytest.mark.parametrize("price", ["5", "500"])
def test_sizing_is_fractional_and_positive_across_capital_and_price_ranges(capital, price):
    """CONSERVATIVE's 5% budget should buy *something* at every capital size
    from $100 to $1,000,000 against both a cheap (~$5) and expensive (~$500)
    stock - the bug this fixes was $100 capital silently sizing to 0."""
    capital = Decimal(capital)
    price = Decimal(price)
    state = _state(cash=capital, equity_peak=capital, current_equity=capital, day_start_equity=capital)

    decision = MANAGER.evaluate(SignalAction.BUY, price, state, CONSERVATIVE)

    assert decision.approved is True
    expected_quantity = (capital * Decimal("0.05") / price).quantize(
        Decimal("0.000001"), rounding=ROUND_DOWN
    )
    assert decision.quantity == expected_quantity
    assert decision.quantity > 0
    # Never spend more than the max_position_pct budget allows, even after rounding.
    assert decision.quantity * price <= capital * Decimal("0.05")


def test_sizing_does_not_overflow_or_misbehave_on_very_large_capital():
    huge_capital = Decimal("1000000000000")  # $1 trillion
    state = _state(
        cash=huge_capital, equity_peak=huge_capital, current_equity=huge_capital, day_start_equity=huge_capital
    )

    decision = MANAGER.evaluate(SignalAction.BUY, Decimal("500"), state, CONSERVATIVE)

    assert decision.approved is True
    assert decision.quantity == huge_capital * Decimal("0.05") / Decimal("500")


# --- halted --------------------------------------------------------------


def test_halted_rejects_a_new_buy():
    decision = MANAGER.evaluate(SignalAction.BUY, Decimal("50"), _state(halted=True), CONSERVATIVE)

    assert decision.approved is False
    assert decision.reason == "Trading is halted"


def test_sell_is_always_allowed_even_while_halted():
    decision = MANAGER.evaluate(SignalAction.SELL, Decimal("50"), _state(halted=True), CONSERVATIVE)

    assert decision == RiskDecision(approved=True)


def test_wait_is_never_approved():
    decision = MANAGER.evaluate(SignalAction.WAIT, Decimal("50"), _state(), CONSERVATIVE)

    assert decision.approved is False


# --- daily loss limit --------------------------------------------------------


def test_daily_loss_limit_breach_rejects_and_signals_halt_for_day():
    # CONSERVATIVE max_daily_loss_pct=0.01; (100000-98000)/100000 = 2% >= 1%
    state = _state(current_equity=Decimal("98000"))
    decision = MANAGER.evaluate(SignalAction.BUY, Decimal("50"), state, CONSERVATIVE)

    assert decision.approved is False
    assert decision.halt_for_day is True
    assert decision.halt_permanently is False


def test_daily_loss_exactly_at_the_threshold_still_rejects():
    # Exactly 1% loss, using >= per spec.
    state = _state(current_equity=Decimal("99000"))
    decision = MANAGER.evaluate(SignalAction.BUY, Decimal("50"), state, CONSERVATIVE)

    assert decision.approved is False
    assert decision.halt_for_day is True


def test_daily_loss_below_the_threshold_does_not_reject_for_that_reason():
    # 0.5% loss, under the 1% CONSERVATIVE threshold.
    state = _state(current_equity=Decimal("99500"))
    decision = MANAGER.evaluate(SignalAction.BUY, Decimal("50"), state, CONSERVATIVE)

    assert decision.approved is True
    assert decision.halt_for_day is False


# --- drawdown circuit breaker -------------------------------------------------


def test_drawdown_breach_rejects_and_signals_permanent_halt():
    # CONSERVATIVE max_drawdown_pct=0.09; flat *today* (day_start==current) but
    # 9% below a prior peak - isolates drawdown from the daily-loss check.
    state = _state(
        equity_peak=Decimal("100000"),
        current_equity=Decimal("91000"),
        day_start_equity=Decimal("91000"),
    )
    decision = MANAGER.evaluate(SignalAction.BUY, Decimal("50"), state, CONSERVATIVE)

    assert decision.approved is False
    assert decision.halt_permanently is True
    assert decision.halt_for_day is False


def test_drawdown_below_the_threshold_does_not_reject_for_that_reason():
    state = _state(
        equity_peak=Decimal("100000"),
        current_equity=Decimal("95000"),
        day_start_equity=Decimal("95000"),
    )
    decision = MANAGER.evaluate(SignalAction.BUY, Decimal("50"), state, CONSERVATIVE)

    assert decision.approved is True
    assert decision.halt_permanently is False


# --- stop-loss / take-profit --------------------------------------------------


def test_stop_loss_and_take_profit_omitted_when_not_required():
    profile = replace(CONSERVATIVE, stop_loss_required=False)
    decision = MANAGER.evaluate(SignalAction.BUY, Decimal("50"), _state(), profile)

    assert decision.approved is True
    assert decision.stop_loss_price is None
    assert decision.take_profit_price is None


def test_halted_takes_precedence_over_sizing():
    """Even with a price/equity combo that would size to a valid quantity,
    a halted account must reject - proving check order, not just outcome."""
    decision = MANAGER.evaluate(SignalAction.BUY, Decimal("50"), _state(halted=True), CONSERVATIVE)

    assert decision.approved is False
    assert decision.quantity is None
    assert decision.reason == "Trading is halted"
