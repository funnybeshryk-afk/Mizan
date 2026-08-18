from __future__ import annotations

from decimal import Decimal
from typing import Sequence


def total_return_pct(initial_capital: Decimal, final_equity: Decimal) -> Decimal:
    """Percent change from initial_capital to final_equity."""
    if initial_capital <= 0:
        raise ValueError("initial_capital must be positive")
    return (final_equity / initial_capital - 1) * 100


def max_drawdown_pct(equity_curve: Sequence[Decimal]) -> Decimal:
    """Largest peak-to-trough decline over the curve, as a positive percent
    (0 if the curve never declines from a prior peak, or has < 2 points)."""
    if len(equity_curve) < 2:
        return Decimal("0")

    peak = equity_curve[0]
    worst = Decimal("0")
    for equity in equity_curve[1:]:
        if equity > peak:
            peak = equity
        elif peak > 0:
            drawdown = (peak - equity) / peak * 100
            if drawdown > worst:
                worst = drawdown
    return worst


def win_rate_pct(closed_trade_pnls: Sequence[Decimal]) -> Decimal:
    """Share of closed trades with positive realized P&L. 0 if there were
    no closed trades (not undefined - an empty backtest didn't "win")."""
    if not closed_trade_pnls:
        return Decimal("0")
    wins = sum(1 for pnl in closed_trade_pnls if pnl > 0)
    return Decimal(wins) / Decimal(len(closed_trade_pnls)) * 100


def num_trades(executed_orders: Sequence) -> int:
    """Count of orders actually executed during the run (BUY and SELL fills
    both count - this is order count, not closed-round-trip count)."""
    return len(executed_orders)
