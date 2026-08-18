from __future__ import annotations

import os
from datetime import date, datetime, time
from decimal import Decimal

from alpaca.common.exceptions import APIError
from alpaca.data.enums import DataFeed
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest, StockLatestTradeRequest
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
from dotenv import load_dotenv
from requests.exceptions import RequestException

from app.core.paths import ENV_PATH
from app.market.provider import (
    AuthenticationError,
    Bar,
    MarketDataError,
    MarketDataProvider,
    NetworkError,
    SymbolNotFoundError,
)

_TIMEFRAME_UNIT_SUFFIXES = {
    "Min": TimeFrameUnit.Minute,
    "Hour": TimeFrameUnit.Hour,
    "Day": TimeFrameUnit.Day,
    "Week": TimeFrameUnit.Week,
    "Month": TimeFrameUnit.Month,
}


def _parse_timeframe(timeframe: str) -> TimeFrame:
    for suffix, unit in _TIMEFRAME_UNIT_SUFFIXES.items():
        if timeframe.endswith(suffix):
            amount_str = timeframe[: -len(suffix)] or "1"
            if amount_str.isdigit():
                return TimeFrame(int(amount_str), unit)
    raise ValueError(f"Unsupported timeframe: {timeframe!r}")


class AlpacaMarketDataProvider(MarketDataProvider):
    """Live US-equity market data from Alpaca's Market Data API.

    Requires ALPACA_API_KEY / ALPACA_SECRET_KEY (see .env.example); both a
    free paper account's keys work since we default to the IEX feed, which
    doesn't need a paid SIP subscription.
    """

    def __init__(self, api_key: str | None = None, secret_key: str | None = None):
        load_dotenv(ENV_PATH)
        api_key = api_key or os.environ.get("ALPACA_API_KEY")
        secret_key = secret_key or os.environ.get("ALPACA_SECRET_KEY")

        if not api_key or not secret_key:
            raise AuthenticationError(
                "ALPACA_API_KEY / ALPACA_SECRET_KEY are not set. Copy .env.example to "
                ".env and fill in your keys (free at https://alpaca.markets)."
            )

        self._client = StockHistoricalDataClient(api_key=api_key, secret_key=secret_key)

    def get_latest_price(self, symbol: str) -> Decimal:
        symbol = symbol.upper().strip()
        request = StockLatestTradeRequest(symbol_or_symbols=symbol, feed=DataFeed.IEX)

        try:
            trades = self._client.get_stock_latest_trade(request)
        except APIError as exc:
            raise self._translate_api_error(symbol, exc) from exc
        except RequestException as exc:
            raise NetworkError(f"Could not reach Alpaca: {exc}") from exc

        trade = trades.get(symbol)
        if trade is None:
            raise SymbolNotFoundError(f"Unknown symbol: {symbol}")
        return Decimal(str(trade.price))

    def get_bars(self, symbol: str, start: date, end: date, timeframe: str = "1Day") -> list[Bar]:
        symbol = symbol.upper().strip()
        request = StockBarsRequest(
            symbol_or_symbols=symbol,
            start=datetime.combine(start, time.min),
            end=datetime.combine(end, time.max),
            timeframe=_parse_timeframe(timeframe),
            feed=DataFeed.IEX,
        )

        try:
            bar_set = self._client.get_stock_bars(request)
        except APIError as exc:
            raise self._translate_api_error(symbol, exc) from exc
        except RequestException as exc:
            raise NetworkError(f"Could not reach Alpaca: {exc}") from exc

        raw_bars = bar_set.data.get(symbol, [])
        return [
            Bar(
                timestamp=bar.timestamp,
                open=Decimal(str(bar.open)),
                high=Decimal(str(bar.high)),
                low=Decimal(str(bar.low)),
                close=Decimal(str(bar.close)),
                volume=int(bar.volume),
            )
            for bar in raw_bars
        ]

    @staticmethod
    def _translate_api_error(symbol: str, exc: APIError) -> MarketDataError:
        status = exc.status_code
        if status in (401, 403):
            return AuthenticationError(f"Alpaca rejected the API credentials: {exc}")
        if status == 404:
            return SymbolNotFoundError(f"Unknown symbol: {symbol}")
        return MarketDataError(f"Alpaca API error (status {status}): {exc}")
