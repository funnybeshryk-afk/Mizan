from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from app.market.provider import Bar
from app.strategies.indicators import ema, rsi, sma


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


# --- SMA ---------------------------------------------------------------
# Hand check for [1,2,3,4,5], period 3:
#   window(1,2,3)=2, window(2,3,4)=3, window(3,4,5)=4


def test_sma_matches_hand_computed_windows():
    result = sma([1, 2, 3, 4, 5], 3)
    assert result == [None, None, Decimal("2"), Decimal("3"), Decimal("4")]


def test_sma_accepts_bar_objects():
    bars = _bars_from_closes([1, 2, 3, 4, 5])
    assert sma(bars, 3) == [None, None, Decimal("2"), Decimal("3"), Decimal("4")]


def test_sma_rejects_non_positive_period():
    with pytest.raises(ValueError):
        sma([1, 2, 3], 0)


def test_sma_all_none_when_not_enough_data():
    assert sma([1, 2], 3) == [None, None]


# --- EMA -----------------------------------------------------------------
# Hand check for [1,2,3,4,5], period 3, multiplier = 2/(3+1) = 0.5:
#   seed (SMA of 1,2,3) = 2
#   ema[3] = (4-2)*0.5 + 2 = 3
#   ema[4] = (5-3)*0.5 + 3 = 4


def test_ema_matches_hand_computed_seed_and_smoothing():
    result = ema([1, 2, 3, 4, 5], 3)
    assert result == [None, None, Decimal("2"), Decimal("3.0"), Decimal("4.00")]


def test_ema_rejects_non_positive_period():
    with pytest.raises(ValueError):
        ema([1, 2, 3], 0)


def test_ema_all_none_when_not_enough_data():
    assert ema([1, 2], 3) == [None, None]


# --- RSI -------------------------------------------------------------------
# Hand-derived with Wilder's method on [1,2,3,2,1,2,3,4], period 3.
# Diffs (gain/loss) for indices 1..7: +1/0, +1/0, 0/-1, 0/-1, +1/0, +1/0, +1/0
# First avg over the first 3 diffs: avg_gain=(1+1+0)/3=2/3, avg_loss=(0+0+1)/3=1/3
#   RS=2 -> RSI = 100 - 100/3 = 200/3 = 66.666...  (lands on price index 3)
# Wilder smoothing (avg = (avg*(period-1) + new)/period) for each following diff:
#   index4: avg_gain=4/9, avg_loss=5/9 -> RS=0.8 -> RSI=400/9=44.444...
#   index5: avg_gain=17/27, avg_loss=10/27 -> RS=1.7 -> RSI=1700/27=62.963...
#   index6: avg_gain=61/81, avg_loss=20/81 -> RS=3.05 -> RSI=6100/81=75.309...
#   index7: avg_gain=203/243, avg_loss=40/243 -> RS=5.075 -> RSI=20300/243=83.539...


def test_rsi_matches_hand_computed_wilder_smoothing():
    result = rsi([1, 2, 3, 2, 1, 2, 3, 4], period=3)

    assert result[0] is None
    assert result[1] is None
    assert result[2] is None
    assert float(result[3]) == pytest.approx(66.6667, abs=1e-3)
    assert float(result[4]) == pytest.approx(44.4444, abs=1e-3)
    assert float(result[5]) == pytest.approx(62.9630, abs=1e-3)
    assert float(result[6]) == pytest.approx(75.3086, abs=1e-3)
    assert float(result[7]) == pytest.approx(83.5391, abs=1e-3)


def test_rsi_is_100_when_there_are_no_losses():
    # Strictly increasing series: avg_loss stays 0, so RSI saturates at 100.
    result = rsi([1, 2, 3, 4, 5], period=3)
    assert result[3] == Decimal("100")
    assert result[4] == Decimal("100")


def test_rsi_rejects_non_positive_period():
    with pytest.raises(ValueError):
        rsi([1, 2, 3], 0)


def test_rsi_all_none_when_not_enough_data():
    assert rsi([1, 2, 3], period=3) == [None, None, None]
