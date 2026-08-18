from __future__ import annotations

from decimal import Decimal

import pytest

from app.broker.paper_broker import PaperBroker
from app.database.models import Account, OrderStatus, TradeSide
from app.database.session import get_engine, get_session_factory
from app.market.provider import MarketDataProvider
from app.portfolio.portfolio import (
    InsufficientFundsError,
    InsufficientPositionError,
    Portfolio,
)


class FakePriceProvider(MarketDataProvider):
    """No-network stand-in with a price you can change between calls."""

    def __init__(self, price: Decimal):
        self.price = price

    def get_latest_price(self, symbol):
        return self.price

    def get_bars(self, symbol, start, end, timeframe="1Day"):
        raise NotImplementedError


@pytest.fixture
def portfolio():
    engine = get_engine("sqlite:///:memory:")
    session_factory = get_session_factory(engine)
    session = session_factory()
    yield Portfolio.create(session, initial_cash=10000)
    session.close()


def test_buy_fills_and_updates_portfolio(portfolio):
    broker = PaperBroker(portfolio, FakePriceProvider(Decimal("100")))
    instrument = portfolio.get_or_create_instrument("AAPL")

    order = broker.submit_order(portfolio.account, instrument, TradeSide.BUY, Decimal("10"))

    assert order.status == OrderStatus.FILLED
    assert order.fill_price == Decimal("100")
    assert order.filled_at is not None

    position = portfolio.get_position("AAPL")
    assert position.quantity == Decimal("10")
    assert position.avg_price == Decimal("100")
    assert portfolio.cash == Decimal("9000")


def test_sell_fills_and_updates_portfolio(portfolio):
    price_provider = FakePriceProvider(Decimal("100"))
    broker = PaperBroker(portfolio, price_provider)
    instrument = portfolio.get_or_create_instrument("AAPL")

    broker.submit_order(portfolio.account, instrument, TradeSide.BUY, Decimal("10"))
    price_provider.price = Decimal("120")
    order = broker.submit_order(portfolio.account, instrument, TradeSide.SELL, Decimal("4"))

    assert order.status == OrderStatus.FILLED
    assert order.fill_price == Decimal("120")

    position = portfolio.get_position("AAPL")
    assert position.quantity == Decimal("6")
    assert portfolio.cash == Decimal("9000") + Decimal("480")


def test_broker_is_a_wrapper_not_a_second_ledger(portfolio):
    """Same inputs through the broker must leave Portfolio in exactly the
    state a direct Portfolio.buy() call would - PaperBroker must not
    reimplement any accounting of its own."""
    broker = PaperBroker(portfolio, FakePriceProvider(Decimal("55")))
    instrument = portfolio.get_or_create_instrument("MSFT")
    broker.submit_order(portfolio.account, instrument, TradeSide.BUY, Decimal("3"))

    direct_portfolio = Portfolio.create(portfolio.session, initial_cash=10000)
    direct_portfolio.buy("MSFT", Decimal("3"), Decimal("55"))

    broker_position = portfolio.get_position("MSFT")
    direct_position = direct_portfolio.get_position("MSFT")
    assert broker_position.quantity == direct_position.quantity
    assert broker_position.avg_price == direct_position.avg_price
    assert portfolio.cash == direct_portfolio.cash


def test_filled_trade_links_back_to_its_order(portfolio):
    broker = PaperBroker(portfolio, FakePriceProvider(Decimal("100")))
    instrument = portfolio.get_or_create_instrument("AAPL")

    order = broker.submit_order(portfolio.account, instrument, TradeSide.BUY, Decimal("2"))

    trades = portfolio.trades()
    assert len(trades) == 1
    assert trades[0].order_id == order.id


def test_buy_rejected_on_insufficient_funds_saves_order_without_a_trade(portfolio):
    broker = PaperBroker(portfolio, FakePriceProvider(Decimal("100")))
    instrument = portfolio.get_or_create_instrument("AAPL")

    with pytest.raises(InsufficientFundsError):
        broker.submit_order(portfolio.account, instrument, TradeSide.BUY, Decimal("1000"))

    orders = broker.orders()
    assert len(orders) == 1
    assert orders[0].status == OrderStatus.REJECTED
    assert portfolio.trades() == []
    assert portfolio.cash == Decimal("10000")


def test_sell_rejected_on_insufficient_position_saves_order_without_a_trade(portfolio):
    broker = PaperBroker(portfolio, FakePriceProvider(Decimal("100")))
    instrument = portfolio.get_or_create_instrument("AAPL")

    with pytest.raises(InsufficientPositionError):
        broker.submit_order(portfolio.account, instrument, TradeSide.SELL, Decimal("5"))

    orders = broker.orders()
    assert len(orders) == 1
    assert orders[0].status == OrderStatus.REJECTED
    assert portfolio.trades() == []


def test_explicit_price_overrides_the_provider(portfolio):
    broker = PaperBroker(portfolio, FakePriceProvider(Decimal("100")))
    instrument = portfolio.get_or_create_instrument("AAPL")

    order = broker.submit_order(
        portfolio.account, instrument, TradeSide.BUY, Decimal("1"), price=Decimal("50")
    )

    assert order.fill_price == Decimal("50")
    assert portfolio.cash == Decimal("9950")


def test_no_provider_requires_an_explicit_price(portfolio):
    broker = PaperBroker(portfolio, market_provider=None)
    instrument = portfolio.get_or_create_instrument("AAPL")

    with pytest.raises(ValueError):
        broker.submit_order(portfolio.account, instrument, TradeSide.BUY, Decimal("1"))


def test_no_provider_with_explicit_price_still_fills(portfolio):
    broker = PaperBroker(portfolio, market_provider=None)
    instrument = portfolio.get_or_create_instrument("AAPL")

    order = broker.submit_order(
        portfolio.account, instrument, TradeSide.BUY, Decimal("1"), price=Decimal("77")
    )

    assert order.status == OrderStatus.FILLED
    assert order.fill_price == Decimal("77")


def test_submit_order_rejects_an_account_it_is_not_bound_to(portfolio):
    other_account = Account(cash=Decimal("500"))
    portfolio.session.add(other_account)
    portfolio.session.commit()

    broker = PaperBroker(portfolio, FakePriceProvider(Decimal("100")))
    instrument = portfolio.get_or_create_instrument("AAPL")

    with pytest.raises(ValueError):
        broker.submit_order(other_account, instrument, TradeSide.BUY, Decimal("1"))


def test_paperbroker_supports_fractional_quantities_without_restriction(portfolio):
    """Unlike a real broker (Alpaca may reject fractional qty for some
    symbols), PaperBroker is our own math and must never round or reject
    a fractional quantity on that basis."""
    broker = PaperBroker(portfolio, FakePriceProvider(Decimal("100")))
    instrument = portfolio.get_or_create_instrument("AAPL")

    order = broker.submit_order(portfolio.account, instrument, TradeSide.BUY, Decimal("0.123456"))

    assert order.status == OrderStatus.FILLED
    position = portfolio.get_position("AAPL")
    assert position.quantity == Decimal("0.123456")


def test_sync_pending_orders_default_is_a_no_op(portfolio):
    """PaperBroker fills synchronously inside submit_order() - it inherits
    Broker's default sync_pending_orders() (added for AlpacaBroker's real
    async reconciliation) rather than overriding it, and that default must
    stay a harmless no-op for PaperBroker."""
    broker = PaperBroker(portfolio, FakePriceProvider(Decimal("100")))

    assert broker.sync_pending_orders() == []
