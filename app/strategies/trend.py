from __future__ import annotations

from typing import Any

from app.database.models import SignalAction
from app.market.provider import Bar
from app.strategies.indicators import ema, rsi
from app.strategies.strategy import Signal, Strategy

DEFAULT_PARAMS: dict[str, Any] = {
    "ema_fast_period": 50,
    "ema_slow_period": 200,
    "rsi_period": 14,
    "rsi_overbought": 70,
}


class TrendStrategy(Strategy):
    """BUY when price is above a fast EMA that is itself above a slow EMA
    (an uptrend) and RSI isn't overbought; SELL when price is below a fast
    EMA that is below the slow EMA (a downtrend); otherwise WAIT.
    """

    def __init__(self, params: dict[str, Any] | None = None):
        merged = dict(DEFAULT_PARAMS)
        merged.update(params or {})
        super().__init__(merged)

    def generate_signal(self, bars: list[Bar]) -> Signal:
        closes = [bar.close for bar in bars]
        last_price = closes[-1] if closes else None

        fast_period = self.params["ema_fast_period"]
        slow_period = self.params["ema_slow_period"]
        rsi_period = self.params["rsi_period"]
        overbought = self.params["rsi_overbought"]

        if len(closes) < slow_period:
            return Signal(action=SignalAction.WAIT, price=last_price)

        ema_fast = ema(closes, fast_period)[-1]
        ema_slow = ema(closes, slow_period)[-1]
        rsi_value = rsi(closes, rsi_period)[-1]

        if ema_fast is None or ema_slow is None:
            return Signal(action=SignalAction.WAIT, price=last_price)

        uptrend = last_price > ema_fast and ema_fast > ema_slow
        downtrend = last_price < ema_fast and ema_fast < ema_slow
        not_overbought = rsi_value is None or rsi_value < overbought

        if uptrend and not_overbought:
            return Signal(action=SignalAction.BUY, price=last_price)
        if downtrend:
            return Signal(action=SignalAction.SELL, price=last_price)
        return Signal(action=SignalAction.WAIT, price=last_price)
