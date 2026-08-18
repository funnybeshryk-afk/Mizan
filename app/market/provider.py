from __future__ import annotations

import abc
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal


class MarketDataError(Exception):
    """Base class for all market-data provider failures."""


class AuthenticationError(MarketDataError):
    """Credentials are missing, malformed, or rejected by the provider."""


class NetworkError(MarketDataError):
    """The provider could not be reached (timeout, DNS, connection refused, ...)."""


class SymbolNotFoundError(MarketDataError):
    """The provider does not recognize the requested symbol."""


@dataclass(frozen=True)
class Bar:
    timestamp: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: int


class MarketDataProvider(abc.ABC):
    """Source of live prices and historical bars for one asset class.

    Kept deliberately narrow so a crypto (or other) provider can be added
    later without touching Portfolio, the UI, or the caching layer.
    """

    @abc.abstractmethod
    def get_latest_price(self, symbol: str) -> Decimal:
        ...

    @abc.abstractmethod
    def get_bars(self, symbol: str, start: date, end: date, timeframe: str = "1Day") -> list[Bar]:
        ...
