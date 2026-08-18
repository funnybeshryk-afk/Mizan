from __future__ import annotations

import random
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from app.backtesting.engine import run_backtest
from app.market.provider import Bar
from app.risk.risk_profile import AGGRESSIVE, CONSERVATIVE
from app.strategies.breakout import BreakoutStrategy
from app.strategies.mean_reversion import MeanReversionStrategy
from app.strategies.trend import TrendStrategy


def _make_bars(n=500, seed=7):
    """Deterministic pseudo-random walk with regime shifts (uptrend, chop,
    downtrend, recovery) - long enough for TrendStrategy's default EMA200,
    and varied enough to actually produce BUY/SELL signals, not just WAIT.
    """
    rng = random.Random(seed)
    price = Decimal("100")
    ts = datetime(2023, 1, 1, tzinfo=timezone.utc)
    regimes = (
        [Decimal("0.35")] * 150
        + [Decimal("0.0")] * 100
        + [Decimal("-0.3")] * 120
        + [Decimal("0.25")] * 130
    )
    bars = []
    for i in range(n):
        drift = regimes[i] if i < len(regimes) else Decimal("0")
        noise = Decimal(str(round(rng.uniform(-1.2, 1.2), 4)))
        open_ = price
        close = max(Decimal("1"), price + drift + noise)
        high = max(open_, close) + Decimal(str(round(rng.uniform(0, 0.6), 4)))
        low = min(open_, close) - Decimal(str(round(rng.uniform(0, 0.6), 4)))
        bars.append(
            Bar(timestamp=ts + timedelta(days=i), open=open_, high=high, low=low, close=close, volume=1000)
        )
        price = close
    return bars


def test_trend_strategy_runs_under_both_profiles_on_the_same_history():
    """This is the first real calibration data point (task's own framing) -
    not a precise-value assertion (the synthetic series has no hand-derivable
    expected result), but a structural check that both profiles complete
    cleanly and actually behave like risk management: CONSERVATIVE's worst
    drawdown must not exceed its own configured ceiling.
    """
    bars = _make_bars()
    initial_capital = Decimal("100000")

    result_none = run_backtest(TrendStrategy(), bars, initial_capital, symbol="SYN", risk_profile=None)
    result_conservative = run_backtest(
        TrendStrategy(), bars, initial_capital, symbol="SYN", risk_profile=CONSERVATIVE
    )
    result_aggressive = run_backtest(
        TrendStrategy(), bars, initial_capital, symbol="SYN", risk_profile=AGGRESSIVE
    )

    for result in (result_none, result_conservative, result_aggressive):
        assert result.final_equity > 0
        assert len(result.equity_curve) == len(bars)

    conservative_equity = [p.equity for p in result_conservative.equity_curve]
    # Recomputed independently here (not by calling app.backtesting.metrics)
    # so this check doesn't just assume the metrics module is correct.
    # O(n^2) but n is small (500) and this is a one-off calibration test.
    worst_dd_pct = max(
        (max(conservative_equity[: i + 1]) - equity) / max(conservative_equity[: i + 1]) * 100
        for i, equity in enumerate(conservative_equity)
        if max(conservative_equity[: i + 1]) > 0
    )
    assert worst_dd_pct <= Decimal(str(CONSERVATIVE.max_drawdown_pct * 100)) + Decimal("0.5")


def test_breakout_strategy_runs_under_both_profiles_on_the_same_history():
    """Same calibration pattern as TrendStrategy's, on the same synthetic
    history - a second real data point now exists to compare strategies
    against, not just risk profiles within one strategy."""
    bars = _make_bars()
    initial_capital = Decimal("100000")

    result_none = run_backtest(BreakoutStrategy(), bars, initial_capital, symbol="SYN", risk_profile=None)
    result_conservative = run_backtest(
        BreakoutStrategy(), bars, initial_capital, symbol="SYN", risk_profile=CONSERVATIVE
    )
    result_aggressive = run_backtest(
        BreakoutStrategy(), bars, initial_capital, symbol="SYN", risk_profile=AGGRESSIVE
    )

    for result in (result_none, result_conservative, result_aggressive):
        assert result.final_equity > 0
        assert len(result.equity_curve) == len(bars)

    conservative_equity = [p.equity for p in result_conservative.equity_curve]
    worst_dd_pct = max(
        (max(conservative_equity[: i + 1]) - equity) / max(conservative_equity[: i + 1]) * 100
        for i, equity in enumerate(conservative_equity)
        if max(conservative_equity[: i + 1]) > 0
    )
    assert worst_dd_pct <= Decimal(str(CONSERVATIVE.max_drawdown_pct * 100)) + Decimal("0.5")


def test_mean_reversion_strategy_runs_under_both_profiles_on_the_same_history():
    """Same calibration pattern as Trend's/Breakout's, on the same
    synthetic history - a third data point to compare strategies against."""
    bars = _make_bars()
    initial_capital = Decimal("100000")

    result_none = run_backtest(MeanReversionStrategy(), bars, initial_capital, symbol="SYN", risk_profile=None)
    result_conservative = run_backtest(
        MeanReversionStrategy(), bars, initial_capital, symbol="SYN", risk_profile=CONSERVATIVE
    )
    result_aggressive = run_backtest(
        MeanReversionStrategy(), bars, initial_capital, symbol="SYN", risk_profile=AGGRESSIVE
    )

    for result in (result_none, result_conservative, result_aggressive):
        assert result.final_equity > 0
        assert len(result.equity_curve) == len(bars)

    conservative_equity = [p.equity for p in result_conservative.equity_curve]
    worst_dd_pct = max(
        (max(conservative_equity[: i + 1]) - equity) / max(conservative_equity[: i + 1]) * 100
        for i, equity in enumerate(conservative_equity)
        if max(conservative_equity[: i + 1]) > 0
    )
    assert worst_dd_pct <= Decimal(str(CONSERVATIVE.max_drawdown_pct * 100)) + Decimal("0.5")
