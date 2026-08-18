from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from numbers import Real

from sqlalchemy import select

from app.broker.broker import Broker
from app.database.models import Account, Instrument, Order, OrderStatus, TradeSide
from app.market.provider import MarketDataProvider
from app.portfolio.portfolio import (
    InsufficientFundsError,
    InsufficientPositionError,
    Portfolio,
)


def _to_decimal(value: Decimal | Real | str) -> Decimal:
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


class PaperBroker(Broker):
    """Fills market orders immediately against a live (or manually supplied)
    price, using Portfolio for all cash/position accounting - this class only
    wraps Portfolio.buy()/sell() with Order bookkeeping, it never duplicates
    the ledger logic itself.

    Known simplification of paper mode: fills happen instantly at the quoted
    price with no slippage and no execution delay. A live broker would not
    get to make that assumption.
    """

    def __init__(self, portfolio: Portfolio, market_provider: MarketDataProvider | None = None):
        self.portfolio = portfolio
        self.session = portfolio.session
        self.market_provider = market_provider

    def submit_order(
        self,
        account: Account,
        instrument: Instrument,
        side: TradeSide,
        quantity: Decimal | Real,
        price: Decimal | Real | None = None,
        commission: Decimal | Real = Decimal("0"),
    ) -> Order:
        if account.id != self.portfolio.account.id:
            raise ValueError(
                "This PaperBroker is bound to a different account than the one supplied"
            )

        quantity = _to_decimal(quantity)
        commission = _to_decimal(commission)

        if price is not None:
            price = _to_decimal(price)
        elif self.market_provider is not None:
            price = self.market_provider.get_latest_price(instrument.symbol)
        else:
            raise ValueError(
                "No market data provider configured for this PaperBroker; "
                "pass an explicit price for a manual paper fill."
            )

        order = Order(
            account_id=account.id,
            instrument_id=instrument.id,
            side=side,
            quantity=quantity,
            order_type="market",
            status=OrderStatus.PENDING,
            commission=commission,
            requested_at=datetime.now(timezone.utc),
        )
        self.session.add(order)
        self.session.flush()

        try:
            if side == TradeSide.BUY:
                trade = self.portfolio.buy(
                    instrument.symbol, quantity, price, commission=commission
                )
            else:
                trade = self.portfolio.sell(
                    instrument.symbol, quantity, price, commission=commission
                )
        except (InsufficientFundsError, InsufficientPositionError):
            order.status = OrderStatus.REJECTED
            self.session.commit()
            raise

        trade.order_id = order.id
        order.status = OrderStatus.FILLED
        order.filled_at = datetime.now(timezone.utc)
        order.fill_price = price
        self.session.commit()
        return order

    def orders(self, limit: int | None = None) -> list[Order]:
        query = (
            select(Order)
            .where(Order.account_id == self.portfolio.account.id)
            .order_by(Order.requested_at.desc(), Order.id.desc())
        )
        if limit is not None:
            query = query.limit(limit)
        return list(self.session.execute(query).scalars())
