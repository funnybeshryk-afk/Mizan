from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from numbers import Real

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database.models import Account, Instrument, Position, Trade, TradeSide


class InsufficientFundsError(Exception):
    """Raised when a buy would spend more cash than the account has free."""


class InsufficientPositionError(Exception):
    """Raised when a sell would reduce a position below zero."""


def _to_decimal(value: Decimal | Real | str) -> Decimal:
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


class Portfolio:
    """Capital and position accounting backed by SQLAlchemy models.

    All money math is done in Decimal to avoid binary-float rounding drift;
    callers may pass plain ints/floats/strings and they are normalized on the way in.
    """

    def __init__(self, session: Session, account: Account):
        self.session = session
        self.account = account

    @classmethod
    def create(cls, session: Session, initial_cash: Decimal | Real = 10000) -> "Portfolio":
        account = Account(cash=_to_decimal(initial_cash))
        session.add(account)
        session.commit()
        return cls(session, account)

    @classmethod
    def load(cls, session: Session, account_id: int) -> "Portfolio":
        account = session.get(Account, account_id)
        if account is None:
            raise ValueError(f"Account {account_id} not found")
        return cls(session, account)

    @property
    def cash(self) -> Decimal:
        return self.account.cash

    def get_or_create_instrument(self, symbol: str) -> Instrument:
        symbol = symbol.upper().strip()
        instrument = self.session.execute(
            select(Instrument).where(Instrument.symbol == symbol)
        ).scalar_one_or_none()
        if instrument is None:
            instrument = Instrument(symbol=symbol, asset_class="US_EQUITY")
            self.session.add(instrument)
            self.session.flush()
        return instrument

    def _get_position(self, instrument: Instrument) -> Position | None:
        return self.session.execute(
            select(Position).where(
                Position.account_id == self.account.id,
                Position.instrument_id == instrument.id,
            )
        ).scalar_one_or_none()

    def get_position(self, symbol: str) -> Position | None:
        instrument = self.get_or_create_instrument(symbol)
        return self._get_position(instrument)

    def positions(self) -> list[Position]:
        return list(
            self.session.execute(
                select(Position).where(
                    Position.account_id == self.account.id,
                    Position.quantity != 0,
                )
            ).scalars()
        )

    def trades(self, limit: int | None = None) -> list[Trade]:
        query = (
            select(Trade)
            .where(Trade.account_id == self.account.id)
            .order_by(Trade.executed_at.desc(), Trade.id.desc())
        )
        if limit is not None:
            query = query.limit(limit)
        return list(self.session.execute(query).scalars())

    def buy(
        self,
        symbol: str,
        quantity: Decimal | Real,
        price: Decimal | Real,
        commission: Decimal | Real = 0,
    ) -> Trade:
        quantity = _to_decimal(quantity)
        price = _to_decimal(price)
        commission = _to_decimal(commission)

        if quantity <= 0:
            raise ValueError("quantity must be positive")
        if price <= 0:
            raise ValueError("price must be positive")
        if commission < 0:
            raise ValueError("commission cannot be negative")

        total_cost = quantity * price + commission
        if total_cost > self.account.cash:
            raise InsufficientFundsError(
                f"Not enough cash: need {total_cost}, have {self.account.cash}"
            )

        instrument = self.get_or_create_instrument(symbol)
        position = self._get_position(instrument)

        if position is None:
            position = Position(
                account_id=self.account.id,
                instrument_id=instrument.id,
                quantity=Decimal("0"),
                avg_price=Decimal("0"),
            )
            self.session.add(position)

        new_quantity = position.quantity + quantity
        position.avg_price = (
            (position.avg_price * position.quantity) + (price * quantity)
        ) / new_quantity
        position.quantity = new_quantity

        self.account.cash -= total_cost

        trade = Trade(
            account_id=self.account.id,
            instrument_id=instrument.id,
            side=TradeSide.BUY,
            quantity=quantity,
            price=price,
            commission=commission,
            executed_at=datetime.now(timezone.utc),
        )
        self.session.add(trade)
        self.session.commit()
        return trade

    def sell(
        self,
        symbol: str,
        quantity: Decimal | Real,
        price: Decimal | Real,
        commission: Decimal | Real = 0,
    ) -> Trade:
        quantity = _to_decimal(quantity)
        price = _to_decimal(price)
        commission = _to_decimal(commission)

        if quantity <= 0:
            raise ValueError("quantity must be positive")
        if price <= 0:
            raise ValueError("price must be positive")
        if commission < 0:
            raise ValueError("commission cannot be negative")

        instrument = self.get_or_create_instrument(symbol)
        position = self._get_position(instrument)
        available = position.quantity if position is not None else Decimal("0")

        if position is None or available < quantity:
            raise InsufficientPositionError(
                f"Not enough shares of {instrument.symbol}: need {quantity}, have {available}"
            )

        position.quantity -= quantity
        if position.quantity == 0:
            position.avg_price = Decimal("0")

        self.account.cash += quantity * price - commission

        trade = Trade(
            account_id=self.account.id,
            instrument_id=instrument.id,
            side=TradeSide.SELL,
            quantity=quantity,
            price=price,
            commission=commission,
            executed_at=datetime.now(timezone.utc),
        )
        self.session.add(trade)
        self.session.commit()
        return trade

    def unrealized_pnl(self, symbol: str, current_price: Decimal | Real) -> Decimal:
        current_price = _to_decimal(current_price)
        position = self.get_position(symbol)
        if position is None or position.quantity == 0:
            return Decimal("0")
        return (current_price - position.avg_price) * position.quantity

    def realized_pnl(self, symbol: str | None = None) -> Decimal:
        """P&L already locked in by closed (sell) trades, using the weighted
        average cost method. Replays the full trade history rather than
        trusting current position state, since Trade is an immutable log
        and the source of truth.
        """
        query = select(Trade).where(Trade.account_id == self.account.id)
        if symbol is not None:
            instrument = self.get_or_create_instrument(symbol)
            query = query.where(Trade.instrument_id == instrument.id)
        query = query.order_by(Trade.executed_at, Trade.id)
        trade_log = list(self.session.execute(query).scalars())

        running: dict[int, tuple[Decimal, Decimal]] = {}
        realized = Decimal("0")

        for trade in trade_log:
            qty, avg_price = running.get(trade.instrument_id, (Decimal("0"), Decimal("0")))

            if trade.side == TradeSide.BUY:
                new_qty = qty + trade.quantity
                avg_price = ((avg_price * qty) + (trade.price * trade.quantity)) / new_qty
                qty = new_qty
            else:
                realized += (trade.price - avg_price) * trade.quantity - trade.commission
                qty -= trade.quantity
                if qty == 0:
                    avg_price = Decimal("0")

            running[trade.instrument_id] = (qty, avg_price)

        return realized

    def market_value(self, current_prices: dict[str, Decimal | Real]) -> Decimal:
        """Cash plus the mark-to-market value of every open position."""
        total = self.account.cash
        for position in self.positions():
            symbol = position.instrument.symbol
            if symbol not in current_prices:
                continue
            total += position.quantity * _to_decimal(current_prices[symbol])
        return total
