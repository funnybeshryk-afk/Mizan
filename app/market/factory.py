from __future__ import annotations

from sqlalchemy.orm import Session

from app.market.cache import CachedMarketDataProvider
from app.market.provider import MarketDataError, MarketDataProvider


def build_market_provider(session: Session) -> MarketDataProvider | None:
    """Best-effort live-price provider, shared by the GUI (main.py) and the
    automation daemon (app/automation/daemon.py) so neither has to
    reimplement Alpaca credential handling.

    Returns None (manual/offline mode) if Alpaca credentials are missing
    or invalid - never raises, so neither caller crashes just because
    market data isn't configured.
    """
    try:
        from app.market.alpaca_provider import AlpacaMarketDataProvider

        return CachedMarketDataProvider(session, AlpacaMarketDataProvider())
    except MarketDataError:
        return None
