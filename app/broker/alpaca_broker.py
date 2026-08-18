from __future__ import annotations

import os
from datetime import datetime, timezone
from decimal import Decimal
from numbers import Real

from alpaca.common.exceptions import APIError
from alpaca.trading.client import TradingClient
from alpaca.trading.enums import OrderSide
from alpaca.trading.enums import OrderStatus as AlpacaOrderStatus
from alpaca.trading.enums import TimeInForce
from alpaca.trading.requests import MarketOrderRequest
from dotenv import load_dotenv
from requests.exceptions import RequestException
from sqlalchemy import select

from app.broker.broker import (
    AuthenticationError,
    Broker,
    BrokerError,
    FractionalSharesNotSupportedError,
    NetworkError,
)
from app.core.paths import ENV_PATH
from app.database.models import Account, Instrument, Order, OrderStatus, TradeSide
from app.portfolio.portfolio import (
    InsufficientFundsError,
    InsufficientPositionError,
    Portfolio,
)

# Statuses Alpaca will never fill from - treated as a hard rejection.
# PARTIALLY_FILLED is deliberately not here: partial-fill accounting (booking
# part of an order while the rest stays open) is out of scope for this stage,
# so a partially filled order is just left PENDING until it's fully filled.
_TERMINAL_REJECTION_STATUSES = {
    AlpacaOrderStatus.CANCELED,
    AlpacaOrderStatus.EXPIRED,
    AlpacaOrderStatus.REJECTED,
    AlpacaOrderStatus.SUSPENDED,
}


def _to_decimal(value: Decimal | Real | str) -> Decimal:
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


class AlpacaBroker(Broker):
    """Sends real orders to Alpaca's *paper* trading endpoint - never live.
    paper=True is hardcoded below, not read from ALPACA_PAPER (that env var
    only affects market data, stage 2) or any other configuration; switching
    to live trading is a deliberate future change, never an accidental one.

    Execution is asynchronous, unlike PaperBroker: submit_order() only
    records the order as PENDING with Alpaca's broker_order_id and never
    touches Portfolio. A real order can sit pending for a while (especially
    outside market hours), so a separate sync_pending_orders() call is what
    checks Alpaca for a fill and only then books it into Portfolio, at
    whatever price Alpaca actually filled at - not whatever price we might
    have expected.

    submit_order() sends a bare side=SELL/BUY MarketOrderRequest with no
    position_intent (BTO/BTC/STO/STC) and never reads the account's own
    shorting_enabled flag - there is nothing at the request level here (or
    anywhere else in this class) that would reject a SELL whose quantity
    exceeds the actual position and stop it from opening a short. Every
    strategy in this project is long-only and none is meant to ever short,
    so the only thing standing between "duplicate SELL" and "accidental
    short position" is app.automation.daemon's own PENDING-order gate
    (_has_pending_order) - not a guarantee from Alpaca or this class.
    """

    def __init__(
        self,
        portfolio: Portfolio,
        api_key: str | None = None,
        secret_key: str | None = None,
        client: TradingClient | None = None,
    ):
        self.portfolio = portfolio
        self.session = portfolio.session

        if client is not None:
            self._client = client
            return

        load_dotenv(ENV_PATH)
        api_key = api_key or os.environ.get("ALPACA_API_KEY")
        secret_key = secret_key or os.environ.get("ALPACA_SECRET_KEY")

        if not api_key or not secret_key:
            raise AuthenticationError(
                "ALPACA_API_KEY / ALPACA_SECRET_KEY are not set. Copy .env.example to "
                ".env and fill in your keys (free at https://alpaca.markets)."
            )

        self._client = TradingClient(api_key=api_key, secret_key=secret_key, paper=True)

    def submit_order(
        self,
        account: Account,
        instrument: Instrument,
        side: TradeSide,
        quantity: Decimal | Real,
        price: Decimal | Real | None = None,
    ) -> Order:
        """price is accepted only so a caller (app.automation.daemon) can
        invoke this polymorphically with PaperBroker, whose fill price
        must be supplied by the caller - Alpaca fills asynchronously at
        whatever price it actually executes at (booked later by
        sync_pending_orders()/_apply_fill()), so any price given here is
        ignored, never used as a limit or a hint.
        """
        if account.id != self.portfolio.account.id:
            raise ValueError(
                "This AlpacaBroker is bound to a different account than the one supplied"
            )

        quantity = _to_decimal(quantity)
        request = MarketOrderRequest(
            symbol=instrument.symbol,
            qty=float(quantity),
            side=OrderSide.BUY if side == TradeSide.BUY else OrderSide.SELL,
            time_in_force=TimeInForce.DAY,
        )

        try:
            alpaca_order = self._client.submit_order(request)
        except APIError as exc:
            raise self._translate_api_error(exc) from exc
        except RequestException as exc:
            raise NetworkError(f"Could not reach Alpaca: {exc}") from exc

        order = Order(
            account_id=account.id,
            instrument_id=instrument.id,
            side=side,
            quantity=quantity,
            order_type="market",
            status=OrderStatus.PENDING,
            requested_at=datetime.now(timezone.utc),
            broker_order_id=str(alpaca_order.id),
        )
        self.session.add(order)
        self.session.commit()
        return order

    def sync_pending_orders(self) -> list[Order]:
        """Checks every locally-PENDING order that has a broker_order_id
        against Alpaca's current status, and returns whichever ones changed
        state this call (empty list if nothing did)."""
        pending_orders = list(
            self.session.execute(
                select(Order).where(
                    Order.account_id == self.portfolio.account.id,
                    Order.status == OrderStatus.PENDING,
                    Order.broker_order_id.is_not(None),
                )
            ).scalars()
        )

        updated = []
        for order in pending_orders:
            try:
                alpaca_order = self._client.get_order_by_id(order.broker_order_id)
            except APIError as exc:
                raise self._translate_api_error(exc) from exc
            except RequestException as exc:
                raise NetworkError(f"Could not reach Alpaca: {exc}") from exc

            if alpaca_order.status == AlpacaOrderStatus.FILLED:
                self._apply_fill(order, alpaca_order)
                updated.append(order)
            elif alpaca_order.status in _TERMINAL_REJECTION_STATUSES:
                order.status = OrderStatus.REJECTED
                self.session.commit()
                updated.append(order)
            # else: still pending/accepted/new/... - leave it as-is.

        return updated

    def _apply_fill(self, order: Order, alpaca_order) -> None:
        fill_price = _to_decimal(alpaca_order.filled_avg_price)
        fill_qty = _to_decimal(alpaca_order.filled_qty)
        instrument = self.session.get(Instrument, order.instrument_id)

        try:
            if order.side == TradeSide.BUY:
                trade = self.portfolio.buy(instrument.symbol, fill_qty, fill_price)
            else:
                trade = self.portfolio.sell(instrument.symbol, fill_qty, fill_price)
        except (InsufficientFundsError, InsufficientPositionError) as exc:
            # The order genuinely filled at Alpaca - we can't pretend it
            # didn't just because our local ledger disagrees. Surface this
            # loudly for manual reconciliation instead of silently dropping
            # the fill or crashing with an unrelated traceback.
            order.status = OrderStatus.REJECTED
            self.session.commit()
            raise BrokerError(
                f"Alpaca filled order {order.broker_order_id} but the local Portfolio "
                f"could not record it ({exc}) - manual reconciliation needed."
            ) from exc

        trade.order_id = order.id
        order.status = OrderStatus.FILLED
        order.filled_at = alpaca_order.filled_at or datetime.now(timezone.utc)
        order.fill_price = fill_price
        self.session.commit()

    def orders(self, limit: int | None = None) -> list[Order]:
        query = (
            select(Order)
            .where(Order.account_id == self.portfolio.account.id)
            .order_by(Order.requested_at.desc(), Order.id.desc())
        )
        if limit is not None:
            query = query.limit(limit)
        return list(self.session.execute(query).scalars())

    @staticmethod
    def _translate_api_error(exc: APIError) -> BrokerError:
        status = exc.status_code
        if status in (401, 403):
            return AuthenticationError(f"Alpaca rejected the API credentials: {exc}")

        message = AlpacaBroker._safe_error_message(exc)
        if "fraction" in message.lower():
            return FractionalSharesNotSupportedError(
                "Alpaca does not support a fractional quantity for this symbol/order "
                f"type - try a whole-number quantity instead. Details: {message}"
            )

        return BrokerError(f"Alpaca API error (status {status}): {exc}")

    @staticmethod
    def _safe_error_message(exc: APIError) -> str:
        """APIError.message assumes a JSON body and can itself raise if the
        response wasn't JSON (e.g. an HTML error page) - fall back to the
        raw text rather than letting that mask the original error."""
        try:
            return exc.message
        except Exception:
            return str(exc)
