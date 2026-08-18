from __future__ import annotations

from decimal import Decimal

import pytest

from app.backtesting.metrics import max_drawdown_pct, num_trades, total_return_pct, win_rate_pct


# --- total_return_pct ------------------------------------------------------


def test_total_return_pct_gain():
    assert total_return_pct(Decimal("10000"), Decimal("12000")) == Decimal("20")


def test_total_return_pct_loss():
    assert total_return_pct(Decimal("10000"), Decimal("9000")) == Decimal("-10")


def test_total_return_pct_unchanged():
    assert total_return_pct(Decimal("10000"), Decimal("10000")) == Decimal("0")


def test_total_return_pct_rejects_non_positive_initial_capital():
    with pytest.raises(ValueError):
        total_return_pct(Decimal("0"), Decimal("100"))


# --- max_drawdown_pct -------------------------------------------------------


def test_max_drawdown_pct_on_a_v_shaped_curve():
    # Peaks at 12000, troughs at 9000: (12000-9000)/12000*100 = 25%
    equity = [Decimal(v) for v in [10000, 11000, 12000, 9000, 9500]]
    assert max_drawdown_pct(equity) == Decimal("25.00")


def test_max_drawdown_pct_is_zero_for_a_strictly_rising_curve():
    equity = [Decimal(v) for v in [10000, 10500, 11000, 12000]]
    assert max_drawdown_pct(equity) == Decimal("0")


def test_max_drawdown_pct_uses_the_worst_drawdown_not_the_last():
    # Drops to 8000 (peak 10000 -> 20% dd), recovers to 15000, dips to 13500 (10% dd).
    # Worst is the first dip, 20%, even though it isn't the final drawdown.
    equity = [Decimal(v) for v in [10000, 8000, 15000, 13500]]
    assert max_drawdown_pct(equity) == Decimal("20.00")


def test_max_drawdown_pct_empty_or_single_point_is_zero():
    assert max_drawdown_pct([]) == Decimal("0")
    assert max_drawdown_pct([Decimal("10000")]) == Decimal("0")


# --- win_rate_pct ------------------------------------------------------------


def test_win_rate_pct_mixed_trades():
    pnls = [Decimal("100"), Decimal("-50"), Decimal("200"), Decimal("-10")]
    assert win_rate_pct(pnls) == Decimal("50")


def test_win_rate_pct_all_winners():
    pnls = [Decimal("100"), Decimal("50")]
    assert win_rate_pct(pnls) == Decimal("100")


def test_win_rate_pct_all_losers():
    pnls = [Decimal("-100"), Decimal("-50")]
    assert win_rate_pct(pnls) == Decimal("0")


def test_win_rate_pct_a_breakeven_trade_does_not_count_as_a_win():
    pnls = [Decimal("0"), Decimal("100")]
    assert win_rate_pct(pnls) == Decimal("50")


def test_win_rate_pct_with_no_closed_trades_is_zero():
    assert win_rate_pct([]) == Decimal("0")


# --- num_trades --------------------------------------------------------------


def test_num_trades_counts_executed_orders():
    assert num_trades([object(), object(), object()]) == 3


def test_num_trades_is_zero_when_nothing_executed():
    assert num_trades([]) == 0
