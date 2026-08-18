from __future__ import annotations

from decimal import Decimal
from typing import Sequence, Union

from app.market.provider import Bar

PriceSeries = Sequence[Union[Decimal, float, int, Bar]]


def _to_decimal(value: Decimal | float | int) -> Decimal:
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


def _extract_closes(values: PriceSeries) -> list[Decimal]:
    """Accepts either a plain price series or a list of Bar - either way,
    returns closing prices as Decimal."""
    return [_to_decimal(v.close if isinstance(v, Bar) else v) for v in values]


def sma(values: PriceSeries, period: int) -> list[Decimal | None]:
    """Simple moving average. result[i] is None until `period` points are
    available (i.e. the first period-1 entries are always None)."""
    if period <= 0:
        raise ValueError("period must be positive")

    closes = _extract_closes(values)
    result: list[Decimal | None] = [None] * len(closes)

    window_sum = Decimal("0")
    for i, price in enumerate(closes):
        window_sum += price
        if i >= period:
            window_sum -= closes[i - period]
        if i >= period - 1:
            result[i] = window_sum / period

    return result


def ema(values: PriceSeries, period: int) -> list[Decimal | None]:
    """Exponential moving average, seeded with the SMA of the first `period`
    points (the common convention), then smoothed with multiplier 2/(period+1).
    result[i] is None before the seed point."""
    if period <= 0:
        raise ValueError("period must be positive")

    closes = _extract_closes(values)
    result: list[Decimal | None] = [None] * len(closes)
    if len(closes) < period:
        return result

    multiplier = Decimal(2) / Decimal(period + 1)
    seed = sum(closes[:period]) / period
    result[period - 1] = seed

    prev = seed
    for i in range(period, len(closes)):
        prev = (closes[i] - prev) * multiplier + prev
        result[i] = prev

    return result


def rsi(values: PriceSeries, period: int = 14) -> list[Decimal | None]:
    """Wilder's RSI: the first `period` average gain/loss is a plain mean of
    the first `period` price changes, then smoothed thereafter. result[i]
    is None until `period` price changes are available (the first `period`
    entries are always None, since that needs `period` + 1 prices)."""
    if period <= 0:
        raise ValueError("period must be positive")

    closes = _extract_closes(values)
    result: list[Decimal | None] = [None] * len(closes)
    if len(closes) <= period:
        return result

    gains: list[Decimal] = []
    losses: list[Decimal] = []
    for i in range(1, len(closes)):
        change = closes[i] - closes[i - 1]
        gains.append(change if change > 0 else Decimal("0"))
        losses.append(-change if change < 0 else Decimal("0"))

    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    result[period] = _rsi_from_averages(avg_gain, avg_loss)

    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
        result[i + 1] = _rsi_from_averages(avg_gain, avg_loss)

    return result


def donchian_upper(values: PriceSeries, period: int) -> list[Decimal | None]:
    """result[i] = the highest close among the `period` bars strictly
    BEFORE index i (closes[i-period:i]) - closes[i] itself is never part
    of its own upper channel. Used by a breakout strategy's entry
    channel, where the current bar must be compared against a boundary it
    had no part in setting. None until `period` prior closes exist."""
    if period <= 0:
        raise ValueError("period must be positive")

    closes = _extract_closes(values)
    result: list[Decimal | None] = [None] * len(closes)
    for i in range(period, len(closes)):
        result[i] = max(closes[i - period : i])
    return result


def donchian_lower(values: PriceSeries, period: int) -> list[Decimal | None]:
    """Same as donchian_upper but the lowest close of the `period` bars
    strictly before index i - a breakout strategy's exit channel."""
    if period <= 0:
        raise ValueError("period must be positive")

    closes = _extract_closes(values)
    result: list[Decimal | None] = [None] * len(closes)
    for i in range(period, len(closes)):
        result[i] = min(closes[i - period : i])
    return result


def bollinger_middle(values: PriceSeries, period: int) -> list[Decimal | None]:
    """result[i] = SMA(period) of the closes strictly BEFORE index i - the
    same "exclude the current bar" convention as donchian_upper/lower, so
    a mean-reversion strategy's bands never include the very bar being
    tested against them. Reuses sma() for the averaging itself (shifted
    by one index) rather than a second moving-average loop: sma(period)
    at index i-1 is already the mean of closes[i-period:i], exactly the
    window this function wants at index i.
    """
    if period <= 0:
        raise ValueError("period must be positive")

    closes = _extract_closes(values)
    sma_values = sma(closes, period)
    result: list[Decimal | None] = [None] * len(closes)
    for i in range(period, len(closes)):
        result[i] = sma_values[i - 1]
    return result


def _rolling_stddev_excluding_current(closes: list[Decimal], period: int) -> list[Decimal | None]:
    """Population standard deviation (divide by `period`, not `period` - 1
    - the standard Bollinger Band convention) of the closes strictly
    before index i. Not part of the public indicators surface - factored
    out purely so bollinger_upper/lower don't each recompute it.
    """
    result: list[Decimal | None] = [None] * len(closes)
    for i in range(period, len(closes)):
        window = closes[i - period : i]
        mean = sum(window) / period
        variance = sum((c - mean) ** 2 for c in window) / period
        result[i] = variance.sqrt()
    return result


def bollinger_upper(values: PriceSeries, period: int, num_std: Decimal | float = 2.0) -> list[Decimal | None]:
    """result[i] = bollinger_middle[i] + num_std * (population stddev of
    the same excluded-current window). Not needed for MeanReversionStrategy's
    own signal (only the lower band and the middle band are), but added
    for completeness alongside bollinger_lower/middle, per the same
    donchian_upper/donchian_lower convention."""
    if period <= 0:
        raise ValueError("period must be positive")

    closes = _extract_closes(values)
    middle = bollinger_middle(closes, period)
    stddev = _rolling_stddev_excluding_current(closes, period)
    num_std = _to_decimal(num_std)

    result: list[Decimal | None] = [None] * len(closes)
    for i in range(period, len(closes)):
        result[i] = middle[i] + num_std * stddev[i]
    return result


def bollinger_lower(values: PriceSeries, period: int, num_std: Decimal | float = 2.0) -> list[Decimal | None]:
    """Same as bollinger_upper but subtracting num_std * stddev - a
    mean-reversion strategy's oversold entry threshold."""
    if period <= 0:
        raise ValueError("period must be positive")

    closes = _extract_closes(values)
    middle = bollinger_middle(closes, period)
    stddev = _rolling_stddev_excluding_current(closes, period)
    num_std = _to_decimal(num_std)

    result: list[Decimal | None] = [None] * len(closes)
    for i in range(period, len(closes)):
        result[i] = middle[i] - num_std * stddev[i]
    return result


def _rsi_from_averages(avg_gain: Decimal, avg_loss: Decimal) -> Decimal:
    if avg_loss == 0:
        return Decimal("100")
    rs = avg_gain / avg_loss
    return Decimal("100") - (Decimal("100") / (Decimal("1") + rs))
