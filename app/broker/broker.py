from __future__ import annotations

import abc
from decimal import Decimal

from app.database.models import Account, Instrument, Order, TradeSide


class BrokerError(Exception):
    """Base class for all broker failures."""


class AuthenticationError(BrokerError):
    """Credentials are missing, malformed, or rejected by the broker."""


class NetworkError(BrokerError):
    """The broker could not be reached (timeout, DNS, connection refused, ...)."""


class OrderNotFoundError(BrokerError):
    """The broker doesn't recognize a previously submitted order id."""


class FractionalSharesNotSupportedError(BrokerError):
    """The broker rejected the order because it doesn't accept a fractional
    quantity for this symbol/order type (not every real broker supports
    fractional shares the way our own PaperBroker math does)."""


class Broker(abc.ABC):
    """Single entry point for turning a trading decision into an Order.

    Both the UI and, later, automated strategies (stage 8) go through this
    interface so neither has to know whether an order is paper or live.

    Only market orders exist at this stage - a limit order needs to track
    unfilled state over time (price monitoring, expiry, cancellation), which
    is separate work left for later.
    """

    @abc.abstractmethod
    def submit_order(
        self,
        account: Account,
        instrument: Instrument,
        side: TradeSide,
        quantity: Decimal,
    ) -> Order:
        ...

    def sync_pending_orders(self) -> list[Order]:
        """Reconciles any order this broker fills asynchronously since the
        last check, returning whichever ones changed state. A broker that
        always fills synchronously inside submit_order() (PaperBroker) has
        nothing to reconcile, so this default is a no-op; a broker with
        real async execution (AlpacaBroker) overrides it."""
        return []
