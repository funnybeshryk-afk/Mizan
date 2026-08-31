from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest
from alpaca.trading.enums import OrderStatus as AlpacaOrderStatus

from app.broker.alpaca_broker import AlpacaBroker
from app.broker.broker import AuthenticationError, BrokerError, FractionalSharesNotSupportedError
from app.database.models import OrderStatus, TradeSide
from app.database.session import get_engine, get_session_factory
from app.portfolio.portfolio import Portfolio


class FakeTradingClient:
    """No-network stand-in for alpaca.trading.client.TradingClient."""

    def __init__(self, raise_on_submit=None, raise_on_get_account=None, account_cash="10000"):
        self.submitted_requests = []
        self._orders: dict[str, SimpleNamespace] = {}
        self._next_id = 1
        self._raise_on_submit = raise_on_submit
        self._raise_on_get_account = raise_on_get_account
        self._account_cash = account_cash

    def submit_order(self, order_data):
        self.submitted_requests.append(order_data)
        if self._raise_on_submit is not None:
            raise self._raise_on_submit
        order_id = str(self._next_id)
        self._next_id += 1
        alpaca_order = SimpleNamespace(
            id=order_id,
            status=AlpacaOrderStatus.PENDING_NEW,
            filled_avg_price=None,
            filled_qty=None,
            filled_at=None,
        )
        self._orders[order_id] = alpaca_order
        return alpaca_order

    def get_order_by_id(self, order_id):
        return self._orders[str(order_id)]

    def get_account(self):
        if self._raise_on_get_account is not None:
            raise self._raise_on_get_account
        return SimpleNamespace(cash=self._account_cash)

    def set_status(self, order_id, status, filled_avg_price=None, filled_qty=None, filled_at=None):
        order = self._orders[str(order_id)]
        order.status = status
        if filled_avg_price is not None:
            order.filled_avg_price = filled_avg_price
        if filled_qty is not None:
            order.filled_qty = filled_qty
        if filled_at is not None:
            order.filled_at = filled_at


@pytest.fixture
def portfolio():
    engine = get_engine("sqlite:///:memory:")
    session_factory = get_session_factory(engine)
    session = session_factory()
    yield Portfolio.create(session, initial_cash=10000)
    session.close()


@pytest.fixture
def fake_client():
    return FakeTradingClient()


@pytest.fixture
def broker(portfolio, fake_client):
    return AlpacaBroker(portfolio, client=fake_client)


# --- submit_order is fire-and-forget, Portfolio untouched -------------------


def test_submit_order_creates_a_pending_order_without_touching_portfolio(portfolio, broker):
    instrument = portfolio.get_or_create_instrument("AAPL")

    order = broker.submit_order(portfolio.account, instrument, TradeSide.BUY, Decimal("10"))

    assert order.status == OrderStatus.PENDING
    assert order.broker_order_id == "1"
    assert order.filled_at is None
    assert order.fill_price is None
    assert portfolio.cash == Decimal("10000")
    assert portfolio.get_position("AAPL") is None
    assert portfolio.trades() == []


def test_submit_order_accepts_and_ignores_an_explicit_price(portfolio, broker, fake_client):
    """app.automation.daemon calls every broker's submit_order()
    polymorphically with price=... (PaperBroker needs it to know the fill
    price) - AlpacaBroker must accept the same call shape even though it
    never uses the value (Alpaca decides the real fill price, booked later
    by sync_pending_orders())."""
    instrument = portfolio.get_or_create_instrument("AAPL")

    order = broker.submit_order(
        portfolio.account, instrument, TradeSide.BUY, Decimal("10"), price=Decimal("999")
    )

    assert order.status == OrderStatus.PENDING
    assert fake_client.submitted_requests[0].qty == 10.0


def test_submit_order_sends_the_right_side_and_quantity(portfolio, broker, fake_client):
    instrument = portfolio.get_or_create_instrument("AAPL")

    broker.submit_order(portfolio.account, instrument, TradeSide.SELL, Decimal("3"))

    assert len(fake_client.submitted_requests) == 1
    request = fake_client.submitted_requests[0]
    assert request.symbol == "AAPL"
    assert request.qty == 3.0
    assert request.side.value == "sell"


# --- sync_pending_orders: filled ---------------------------------------------


def test_sync_fills_using_the_brokers_price_not_an_assumed_one(portfolio, broker, fake_client):
    instrument = portfolio.get_or_create_instrument("AAPL")
    order = broker.submit_order(portfolio.account, instrument, TradeSide.BUY, Decimal("10"))

    # A price the broker actually reports - deliberately not a "nice" number
    # we might otherwise assume, to prove it's read from the mock's response.
    filled_at = datetime(2024, 3, 1, 14, 30, tzinfo=timezone.utc)
    fake_client.set_status(
        order.broker_order_id,
        AlpacaOrderStatus.FILLED,
        filled_avg_price="105.37",
        filled_qty="10",
        filled_at=filled_at,
    )

    updated = broker.sync_pending_orders()

    assert len(updated) == 1
    assert updated[0].id == order.id
    assert order.status == OrderStatus.FILLED
    assert order.fill_price == Decimal("105.37")
    assert order.filled_at == filled_at

    position = portfolio.get_position("AAPL")
    assert position.quantity == Decimal("10")
    assert position.avg_price == Decimal("105.37")
    assert portfolio.cash == Decimal("10000") - Decimal("10") * Decimal("105.37")

    trades = portfolio.trades()
    assert len(trades) == 1
    assert trades[0].order_id == order.id
    assert trades[0].price == Decimal("105.37")


def test_sync_fill_quantity_comes_from_the_broker_not_the_original_request(portfolio, broker, fake_client):
    """A real broker could in principle report a different filled_qty than
    requested (e.g. partial handling upstream) - the sync path must book
    whatever the broker says was actually filled."""
    instrument = portfolio.get_or_create_instrument("AAPL")
    order = broker.submit_order(portfolio.account, instrument, TradeSide.BUY, Decimal("10"))

    fake_client.set_status(
        order.broker_order_id,
        AlpacaOrderStatus.FILLED,
        filled_avg_price="100.00",
        filled_qty="10",
        filled_at=datetime.now(timezone.utc),
    )
    broker.sync_pending_orders()

    position = portfolio.get_position("AAPL")
    assert position.quantity == Decimal("10")


# --- sync_pending_orders: still pending --------------------------------------


def test_sync_leaves_a_still_pending_order_untouched(portfolio, broker, fake_client):
    instrument = portfolio.get_or_create_instrument("AAPL")
    order = broker.submit_order(portfolio.account, instrument, TradeSide.BUY, Decimal("10"))
    # FakeTradingClient starts orders as PENDING_NEW already - nothing to change.

    updated = broker.sync_pending_orders()

    assert updated == []
    assert order.status == OrderStatus.PENDING
    assert order.fill_price is None
    assert portfolio.cash == Decimal("10000")
    assert portfolio.trades() == []


# --- sync_pending_orders: rejected --------------------------------------------


def test_sync_marks_a_broker_rejected_order_without_creating_a_trade(portfolio, broker, fake_client):
    instrument = portfolio.get_or_create_instrument("AAPL")
    order = broker.submit_order(portfolio.account, instrument, TradeSide.BUY, Decimal("10"))

    fake_client.set_status(order.broker_order_id, AlpacaOrderStatus.REJECTED)

    updated = broker.sync_pending_orders()

    assert len(updated) == 1
    assert order.status == OrderStatus.REJECTED
    assert portfolio.cash == Decimal("10000")
    assert portfolio.trades() == []


def test_sync_marks_a_broker_canceled_order_as_rejected(portfolio, broker, fake_client):
    instrument = portfolio.get_or_create_instrument("AAPL")
    order = broker.submit_order(portfolio.account, instrument, TradeSide.BUY, Decimal("10"))

    fake_client.set_status(order.broker_order_id, AlpacaOrderStatus.CANCELED)

    broker.sync_pending_orders()

    assert order.status == OrderStatus.REJECTED
    assert portfolio.trades() == []


def test_sync_processes_multiple_pending_orders_independently(portfolio, broker, fake_client):
    instrument = portfolio.get_or_create_instrument("AAPL")
    order_a = broker.submit_order(portfolio.account, instrument, TradeSide.BUY, Decimal("1"))
    order_b = broker.submit_order(portfolio.account, instrument, TradeSide.BUY, Decimal("2"))

    fake_client.set_status(
        order_a.broker_order_id,
        AlpacaOrderStatus.FILLED,
        filled_avg_price="50.00",
        filled_qty="1",
        filled_at=datetime.now(timezone.utc),
    )
    fake_client.set_status(order_b.broker_order_id, AlpacaOrderStatus.REJECTED)

    updated = broker.sync_pending_orders()

    assert {o.id for o in updated} == {order_a.id, order_b.id}
    assert order_a.status == OrderStatus.FILLED
    assert order_b.status == OrderStatus.REJECTED


# --- fractional shares --------------------------------------------------------


def test_submit_order_sends_a_fractional_quantity_without_rounding(portfolio, broker, fake_client):
    """AlpacaBroker itself places no whole-share restriction - it's up to
    Alpaca to accept or reject a fractional qty for a given symbol."""
    instrument = portfolio.get_or_create_instrument("AAPL")

    broker.submit_order(portfolio.account, instrument, TradeSide.BUY, Decimal("0.5"))

    assert fake_client.submitted_requests[0].qty == 0.5


def test_fractional_share_rejection_is_translated_to_a_clear_typed_error(portfolio):
    import json

    from alpaca.common.exceptions import APIError

    body = json.dumps(
        {"code": 40310000, "message": "fractional orders are not supported for this asset class"}
    )
    fake_client = FakeTradingClient(raise_on_submit=APIError(body))
    broker = AlpacaBroker(portfolio, client=fake_client)
    instrument = portfolio.get_or_create_instrument("AAPL")

    with pytest.raises(FractionalSharesNotSupportedError):
        broker.submit_order(portfolio.account, instrument, TradeSide.BUY, Decimal("0.5"))

    # The rejected attempt must not have created any local Order or touched cash.
    assert broker.orders() == []
    assert portfolio.cash == Decimal("10000")


# --- credential guard ---------------------------------------------------------


def test_missing_credentials_raise_authentication_error(monkeypatch, portfolio):
    import app.broker.alpaca_broker as alpaca_broker_module

    # A real local .env (e.g. one the developer added with live keys) would
    # otherwise repopulate these vars via load_dotenv() after we clear them -
    # stub it out so this test is deterministic regardless of what's on disk.
    monkeypatch.setattr(alpaca_broker_module, "load_dotenv", lambda *a, **k: None)
    monkeypatch.delenv("ALPACA_API_KEY", raising=False)
    monkeypatch.delenv("ALPACA_SECRET_KEY", raising=False)

    with pytest.raises(AuthenticationError):
        AlpacaBroker(portfolio, api_key=None, secret_key=None)


# --- paper=True is hardcoded, never read from configuration ------------------


def test_trading_client_is_always_constructed_with_paper_true(monkeypatch, portfolio):
    import app.broker.alpaca_broker as alpaca_broker_module

    captured = {}

    class SpyTradingClient:
        def __init__(self, api_key=None, secret_key=None, paper=None, **kwargs):
            captured["api_key"] = api_key
            captured["paper"] = paper

    monkeypatch.setattr(alpaca_broker_module, "TradingClient", SpyTradingClient)
    monkeypatch.setenv("ALPACA_PAPER", "false")

    AlpacaBroker(portfolio, api_key="fake-key", secret_key="fake-secret")

    assert captured["paper"] is True
    assert captured["api_key"] == "fake-key"


# --- get_account_cash ---------------------------------------------------------


def test_get_account_cash_returns_the_brokers_real_cash_balance(portfolio):
    fake_client = FakeTradingClient(account_cash="54321.55")
    broker = AlpacaBroker(portfolio, client=fake_client)

    assert broker.get_account_cash() == Decimal("54321.55")


def test_get_account_cash_translates_an_api_error(portfolio):
    import json

    from alpaca.common.exceptions import APIError

    body = json.dumps({"code": 40010001, "message": "request is not authorized"})
    fake_client = FakeTradingClient(raise_on_get_account=APIError(body))
    broker = AlpacaBroker(portfolio, client=fake_client)

    with pytest.raises(BrokerError):
        broker.get_account_cash()


def test_get_account_cash_translates_a_network_error(portfolio):
    from requests.exceptions import ConnectionError as RequestsConnectionError

    fake_client = FakeTradingClient(raise_on_get_account=RequestsConnectionError("boom"))
    broker = AlpacaBroker(portfolio, client=fake_client)

    with pytest.raises(BrokerError):
        broker.get_account_cash()


def test_broker_bound_to_a_different_account_is_rejected(portfolio, broker):
    from app.database.models import Account

    other_account = Account(cash=Decimal("500"))
    portfolio.session.add(other_account)
    portfolio.session.commit()
    instrument = portfolio.get_or_create_instrument("AAPL")

    with pytest.raises(ValueError):
        broker.submit_order(other_account, instrument, TradeSide.BUY, Decimal("1"))
