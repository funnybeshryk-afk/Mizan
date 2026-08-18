from __future__ import annotations

import inspect
import logging
from datetime import datetime, timedelta, timezone
from decimal import ROUND_DOWN, Decimal

import pytest
from sqlalchemy import select

import app.automation.daemon as daemon_module
from app.automation.daemon import Clock, _execute_signal, build_bot, load_enabled_bots, run_bot_cycle, run_daemon
from app.automation.setup_bots import BotSpec, create_bot
from app.database.models import Account, BotConfig, Order, OrderStatus, SignalAction, TradeSide
from app.database.models import Signal as SignalRecord
from app.database.session import get_engine, get_session_factory
from app.market.provider import Bar, MarketDataProvider
from app.strategies.strategy import Signal, Strategy

SMALL_PARAMS = {
    "ema_fast_period": 5,
    "ema_slow_period": 10,
    "rsi_period": 5,
    "rsi_overbought": 70,
}

# Same shape of data as tests/test_signal_runner.py: enough bars to clear
# ema_slow_period=10, ending in a clean uptrend -> TrendStrategy emits BUY.
UPTREND_CLOSES = [100, 101, 99, 102, 100, 103, 101, 104, 102, 105, 103, 106, 104, 107, 105, 108, 106, 109]
FLAT_CLOSES = [100] * 18


def _bars_from_closes(closes: list[int]) -> list[Bar]:
    ts = datetime(2024, 1, 1, tzinfo=timezone.utc)
    bars = []
    for i, close in enumerate(closes):
        price = Decimal(str(close))
        bars.append(
            Bar(timestamp=ts + timedelta(days=i), open=price, high=price, low=price, close=price, volume=1000)
        )
    return bars


class FakeMarketDataProvider(MarketDataProvider):
    """No-network stand-in: serves a fixed bar list per symbol and records
    every get_bars() call so tests can assert on polling frequency."""

    def __init__(self, bars_by_symbol: dict[str, list[Bar]]):
        self.bars_by_symbol = bars_by_symbol
        self.calls: list[str] = []
        self.call_details: list[tuple] = []

    def get_latest_price(self, symbol: str) -> Decimal:
        return self.bars_by_symbol[symbol][-1].close

    def get_bars(self, symbol: str, start, end, timeframe: str = "1Day") -> list[Bar]:
        self.calls.append(symbol)
        self.call_details.append((symbol, start, end, timeframe))
        return self.bars_by_symbol.get(symbol, [])


class FakeClock(Clock):
    """Deterministic clock: monotonic() and now() only advance when sleep()
    is called, and sleep() never actually blocks - so tests never wait on
    real wall-clock time or a real poll_interval_seconds."""

    def __init__(self, start_date: datetime | None = None):
        self._monotonic = 0.0
        self._now = start_date or datetime(2024, 6, 1, tzinfo=timezone.utc)

    def now(self) -> datetime:
        return self._now

    def monotonic(self) -> float:
        return self._monotonic

    def sleep(self, seconds: float) -> None:
        self._monotonic += seconds
        self._now += timedelta(seconds=seconds)


class FakeAsyncBroker:
    """Mimics AlpacaBroker's asynchronous submit-then-sync lifecycle
    without touching a real API: submit_order() creates a PENDING Order
    and never touches Portfolio (fire-and-forget, exactly like
    AlpacaBroker.submit_order() - see app/broker/alpaca_broker.py).
    sync_pending_orders() never resolves anything on its own here -
    resolve_pending() is the test-only hook that simulates a real sync
    call later discovering a fill or a terminal rejection.
    """

    def __init__(self, portfolio):
        self.portfolio = portfolio
        self.session = portfolio.session

    def submit_order(self, account, instrument, side, quantity, price=None):
        order = Order(
            account_id=account.id,
            instrument_id=instrument.id,
            side=side,
            quantity=quantity,
            order_type="market",
            status=OrderStatus.PENDING,
            requested_at=datetime.now(timezone.utc),
            broker_order_id=f"fake-{quantity}-{side.value}",
        )
        self.session.add(order)
        self.session.commit()
        return order

    def sync_pending_orders(self):
        return []

    def orders(self, limit=None):
        query = (
            select(Order)
            .where(Order.account_id == self.portfolio.account.id)
            .order_by(Order.id.desc())
        )
        if limit is not None:
            query = query.limit(limit)
        return list(self.session.execute(query).scalars())

    def resolve_pending(self, order: Order, *, fill_price=None, rejected: bool = False) -> None:
        """Test-only: does what a real sync_pending_orders() would do once
        the broker reports a terminal state for this order."""
        if rejected:
            order.status = OrderStatus.REJECTED
            self.session.commit()
            return

        instrument = self.session.get(daemon_module.Instrument, order.instrument_id)
        if order.side == TradeSide.BUY:
            trade = self.portfolio.buy(instrument.symbol, order.quantity, fill_price)
        else:
            trade = self.portfolio.sell(instrument.symbol, order.quantity, fill_price)
        trade.order_id = order.id
        order.status = OrderStatus.FILLED
        order.filled_at = datetime.now(timezone.utc)
        order.fill_price = fill_price
        self.session.commit()


@pytest.fixture
def session():
    engine = get_engine("sqlite:///:memory:")
    session_factory = get_session_factory(engine)
    session = session_factory()
    yield session
    session.close()


def _make_bot_config(
    session, *, name, symbol, risk_profile_name, poll_interval_seconds=60, force_session_close=False
):
    spec = BotSpec(
        name=name,
        strategy_class="TrendStrategy",
        risk_profile_name=risk_profile_name,
        symbol=symbol,
        poll_interval_seconds=poll_interval_seconds,
        initial_cash=Decimal("10000"),
        strategy_params=SMALL_PARAMS,
        force_session_close=force_session_close,
    )
    return create_bot(session, spec)


def _expected_conservative_quantity(price: Decimal, equity: Decimal = Decimal("10000")) -> Decimal:
    """Mirrors RiskManager's sizing formula (equity * max_position_pct / price,
    rounded down) purely so the test can assert on the resulting numbers -
    not a reimplementation of the risk decision itself."""
    raw = equity * Decimal("0.05") / price
    return raw.quantize(Decimal("0.000001"), rounding=ROUND_DOWN)


def test_run_bot_cycle_wait_signal_takes_no_action(session, caplog):
    bot_config = _make_bot_config(session, name="conservative-aapl", symbol="AAPL", risk_profile_name="CONSERVATIVE")
    provider = FakeMarketDataProvider({"AAPL": _bars_from_closes(FLAT_CLOSES)})
    bot = build_bot(session, bot_config, provider)
    logger = logging.getLogger("test.daemon.wait")

    with caplog.at_level(logging.INFO, logger="test.daemon.wait"):
        run_bot_cycle(bot, FakeClock(), logger)

    assert bot.portfolio.cash == Decimal("10000")
    assert bot.portfolio.get_position("AAPL") is None
    assert "WAIT" in caplog.text


def test_run_bot_cycle_buy_signal_executes_an_order(session, caplog):
    bot_config = _make_bot_config(session, name="conservative-aapl", symbol="AAPL", risk_profile_name="CONSERVATIVE")
    provider = FakeMarketDataProvider({"AAPL": _bars_from_closes(UPTREND_CLOSES)})
    bot = build_bot(session, bot_config, provider)
    logger = logging.getLogger("test.daemon.buy")

    with caplog.at_level(logging.INFO, logger="test.daemon.buy"):
        run_bot_cycle(bot, FakeClock(), logger)

    price = Decimal("109")
    expected_qty = _expected_conservative_quantity(price)
    position = bot.portfolio.get_position("AAPL")

    assert expected_qty > 0
    assert position.quantity == expected_qty
    assert bot.portfolio.cash == Decimal("10000") - expected_qty * price
    assert len(bot.broker.orders()) == 1
    assert "filled" in caplog.text


def test_run_bot_cycle_does_not_pyramid_on_a_second_buy_signal(session):
    bot_config = _make_bot_config(session, name="conservative-aapl", symbol="AAPL", risk_profile_name="CONSERVATIVE")
    provider = FakeMarketDataProvider({"AAPL": _bars_from_closes(UPTREND_CLOSES)})
    bot = build_bot(session, bot_config, provider)
    logger = logging.getLogger("test.daemon.pyramid")

    run_bot_cycle(bot, FakeClock(), logger)
    first_quantity = bot.portfolio.get_position("AAPL").quantity
    first_cash = bot.portfolio.cash

    run_bot_cycle(bot, FakeClock(), logger)

    assert bot.portfolio.get_position("AAPL").quantity == first_quantity
    assert bot.portfolio.cash == first_cash
    assert len(bot.broker.orders()) == 1


def test_run_bot_cycle_defaults_to_daily_timeframe_and_the_original_400_day_lookback(session):
    # Regression guard: a BotConfig created without an explicit timeframe
    # (exactly how all 4 live bots were created) must keep requesting
    # "1Day" with the same 400-day window as before this change, unchanged.
    bot_config = _make_bot_config(session, name="conservative-aapl", symbol="AAPL", risk_profile_name="CONSERVATIVE")
    assert bot_config.timeframe == "1Day"

    provider = FakeMarketDataProvider({"AAPL": _bars_from_closes(FLAT_CLOSES)})
    bot = build_bot(session, bot_config, provider)
    clock = FakeClock()

    run_bot_cycle(bot, clock, logging.getLogger("test.daemon.timeframe_default"))

    assert len(provider.call_details) == 1
    symbol, start, end, timeframe = provider.call_details[0]
    assert symbol == "AAPL"
    assert timeframe == "1Day"
    assert end == clock.now().date()
    assert (end - start).days == 400


def test_run_bot_cycle_uses_an_explicit_non_daily_timeframe_and_its_own_lookback(session):
    # Not just "the field exists" - it must actually flow through to the
    # MarketDataProvider.get_bars() call, with a lookback window derived
    # from that timeframe rather than the daily 400-day default.
    spec = BotSpec(
        name="intraday-test",
        strategy_class="TrendStrategy",
        risk_profile_name="CONSERVATIVE",
        symbol="AAPL",
        timeframe="1Hour",
        initial_cash=Decimal("10000"),
        strategy_params=SMALL_PARAMS,
    )
    bot_config = create_bot(session, spec)
    assert bot_config.timeframe == "1Hour"

    provider = FakeMarketDataProvider({"AAPL": _bars_from_closes(FLAT_CLOSES)})
    bot = build_bot(session, bot_config, provider)
    clock = FakeClock()

    run_bot_cycle(bot, clock, logging.getLogger("test.daemon.timeframe_hourly"))

    assert len(provider.call_details) == 1
    symbol, start, end, timeframe = provider.call_details[0]
    assert symbol == "AAPL"
    assert timeframe == "1Hour"
    expected_lookback = daemon_module.HISTORY_LOOKBACK_DAYS_BY_TIMEFRAME_UNIT["Hour"]
    assert (end - start).days == expected_lookback
    assert expected_lookback != 400  # must not silently fall back to the daily window


def test_run_daemon_polls_bots_on_independent_cadence(session):
    fast_config = _make_bot_config(
        session, name="fast-bot", symbol="AAPL", risk_profile_name="CONSERVATIVE", poll_interval_seconds=10
    )
    slow_config = _make_bot_config(
        session, name="slow-bot", symbol="TSLA", risk_profile_name="AGGRESSIVE", poll_interval_seconds=25
    )
    provider = FakeMarketDataProvider(
        {"AAPL": _bars_from_closes(FLAT_CLOSES), "TSLA": _bars_from_closes(FLAT_CLOSES)}
    )
    clock = FakeClock()
    logger = logging.getLogger("test.daemon.cadence")

    run_daemon(
        session_factory=lambda: session,
        market_provider_factory=lambda s: provider,
        clock=clock,
        logger=logger,
        tick_seconds=10,
        max_ticks=4,
    )

    fast_calls = provider.calls.count("AAPL")
    slow_calls = provider.calls.count("TSLA")
    assert fast_calls > slow_calls
    assert fast_calls > 0
    assert slow_calls > 0


def test_run_daemon_keeps_two_bots_accounts_fully_separate(session):
    conservative_config = _make_bot_config(
        session, name="conservative-aapl", symbol="AAPL", risk_profile_name="CONSERVATIVE"
    )
    aggressive_config = _make_bot_config(
        session, name="aggressive-tsla", symbol="TSLA", risk_profile_name="AGGRESSIVE"
    )
    assert conservative_config.account_id != aggressive_config.account_id

    provider = FakeMarketDataProvider(
        {"AAPL": _bars_from_closes(UPTREND_CLOSES), "TSLA": _bars_from_closes(UPTREND_CLOSES)}
    )
    logger = logging.getLogger("test.daemon.separation")

    run_daemon(
        session_factory=lambda: session,
        market_provider_factory=lambda s: provider,
        clock=FakeClock(),
        logger=logger,
        max_ticks=1,
    )

    conservative_account = session.get(Account, conservative_config.account_id)
    aggressive_account = session.get(Account, aggressive_config.account_id)

    price = Decimal("109")
    conservative_qty = _expected_conservative_quantity(price)
    aggressive_raw = Decimal("10000") * Decimal("0.18") / price
    aggressive_qty = aggressive_raw.quantize(Decimal("0.000001"), rounding=ROUND_DOWN)

    # Both bots bought - but each against its own account's own starting
    # cash, sized by its own risk profile. Neither trade affected the
    # other's cash or position at all.
    assert conservative_account.cash == Decimal("10000") - conservative_qty * price
    assert aggressive_account.cash == Decimal("10000") - aggressive_qty * price
    assert conservative_qty != aggressive_qty  # different risk profiles size differently
    assert conservative_account.id != aggressive_account.id


def test_daemon_module_never_passes_a_paper_argument_to_a_broker():
    """Structural guard for the paper-only constraint, updated for per-bot
    broker selection (BotConfig.broker): AlpacaBroker is now a legitimate
    broker daemon.py can construct (for broker="alpaca_paper"), so
    "alpaca is never mentioned" is no longer the right invariant to check.
    What must still hold is that daemon.py can never make a live trade
    happen: AlpacaBroker hardcodes paper=True internally whenever it isn't
    given an explicit client (see test_alpaca_broker.py::
    test_trading_client_is_always_constructed_with_paper_true) - so as
    long as daemon.py itself never passes a `paper` argument of its own
    to any broker constructor, an accidental paper=False is
    architecturally unreachable from here, not just unlikely.
    """
    source = inspect.getsource(daemon_module)
    assert "paper=" not in source
    assert "paper =" not in source


def test_alpaca_paper_bot_constructs_alpacabroker_with_no_paper_argument_at_all(session, monkeypatch):
    """Dynamic complement to the static source check above: proves
    build_bot() itself never supplies a `paper` argument, by spying on the
    exact constructor call - not just grepping for the substring.
    """
    captured = {}

    class SpyAlpacaBroker:
        def __init__(self, portfolio, **kwargs):
            captured["kwargs"] = kwargs

    monkeypatch.setattr(daemon_module, "AlpacaBroker", SpyAlpacaBroker)

    spec = BotSpec(
        name="alpaca-paper-test",
        strategy_class="TrendStrategy",
        risk_profile_name="CONSERVATIVE",
        symbol="AAPL",
        broker="alpaca_paper",
    )
    bot_config = create_bot(session, spec)
    bot = build_bot(session, bot_config, market_provider=None)

    assert isinstance(bot.broker, SpyAlpacaBroker)
    assert "paper" not in captured["kwargs"]


def test_default_broker_bot_still_constructs_a_paperbroker(session):
    """Regression: a BotConfig without an explicit broker (or broker=
    "paper", exactly how all 4 live bots were created) must keep
    constructing PaperBroker, unchanged."""
    bot_config = _make_bot_config(session, name="conservative-aapl", symbol="AAPL", risk_profile_name="CONSERVATIVE")
    assert bot_config.broker == "paper"

    provider = FakeMarketDataProvider({"AAPL": _bars_from_closes(FLAT_CLOSES)})
    bot = build_bot(session, bot_config, provider)

    assert isinstance(bot.broker, daemon_module.PaperBroker)


def test_run_bot_cycle_calls_sync_pending_orders_before_evaluating_the_strategy(session):
    """The gap this closes: AlpacaBroker.submit_order() is fire-and-forget
    (never touches Portfolio) - without a sync call each cycle, an
    alpaca_paper bot's position would never reflect a real fill, breaking
    pyramiding checks and equity tracking. Confirms run_bot_cycle() reaches
    into the broker to reconcile before doing anything position-dependent.
    """
    calls = []

    class SpyBroker:
        def sync_pending_orders(self):
            calls.append("sync")
            return []

        def submit_order(self, *a, **k):
            raise AssertionError("should not be called - the strategy WAITs on flat closes")

        def orders(self, limit=None):
            return []

    bot_config = _make_bot_config(session, name="conservative-aapl", symbol="AAPL", risk_profile_name="CONSERVATIVE")
    provider = FakeMarketDataProvider({"AAPL": _bars_from_closes(FLAT_CLOSES)})
    bot = build_bot(session, bot_config, provider)
    bot.broker = SpyBroker()

    run_bot_cycle(bot, FakeClock(), logging.getLogger("test.daemon.sync"))

    assert calls == ["sync"]


def test_run_bot_cycle_skips_the_rest_of_the_cycle_if_sync_fails(session, caplog):
    from app.broker.broker import BrokerError

    class FailingSyncBroker:
        def sync_pending_orders(self):
            raise BrokerError("Alpaca unreachable")

        def submit_order(self, *a, **k):
            raise AssertionError("should not be reached - sync failed first")

        def orders(self, limit=None):
            return []

    bot_config = _make_bot_config(session, name="conservative-aapl", symbol="AAPL", risk_profile_name="CONSERVATIVE")
    provider = FakeMarketDataProvider({"AAPL": _bars_from_closes(FLAT_CLOSES)})
    bot = build_bot(session, bot_config, provider)
    bot.broker = FailingSyncBroker()
    logger = logging.getLogger("test.daemon.sync_fail")

    with caplog.at_level(logging.WARNING, logger="test.daemon.sync_fail"):
        run_bot_cycle(bot, FakeClock(), logger)

    assert provider.calls == []  # never even reached the market-data fetch
    assert "failed to sync pending orders" in caplog.text


# --- Stage 13 Part A follow-up: gate a second BUY on a still-pending one ----


def test_second_buy_is_not_sent_while_the_first_buy_order_is_still_pending(session, caplog):
    """Direct reproduction of the found gap: AlpacaBroker.submit_order()
    is fire-and-forget, so Portfolio never sees the position while the
    order is still PENDING at the broker - current_qty alone stays 0. A
    stateless strategy (like OpeningRangeBreakoutStrategy) re-issuing BUY
    on the next cycle must not stack a second real order on top of the
    unconfirmed first one.
    """
    bot_config = _make_bot_config(session, name="intraday-test", symbol="AAPL", risk_profile_name="CONSERVATIVE")
    provider = FakeMarketDataProvider({"AAPL": _bars_from_closes(FLAT_CLOSES)})
    bot = build_bot(session, bot_config, provider)
    bot.broker = FakeAsyncBroker(bot.portfolio)
    bot.strategy = AlwaysReturnStrategy(SignalAction.BUY, Decimal("100"))
    logger = logging.getLogger("test.daemon.pending_gate")

    run_bot_cycle(bot, FakeClock(), logger)  # first cycle: submits the BUY
    assert len(bot.broker.orders()) == 1
    assert bot.broker.orders()[0].status == OrderStatus.PENDING
    assert bot.portfolio.get_position("AAPL") is None  # still unconfirmed

    with caplog.at_level(logging.INFO, logger="test.daemon.pending_gate"):
        run_bot_cycle(bot, FakeClock(), logger)  # second cycle: still pending, strategy says BUY again

    assert len(bot.broker.orders()) == 1  # no second order was sent
    assert "still pending at the broker" in caplog.text


def test_buy_is_gated_normally_by_current_qty_once_the_pending_order_fills(session):
    """Once sync_pending_orders() (simulated here via resolve_pending())
    confirms the fill, the position is real - the ordinary current_qty > 0
    anti-pyramiding check takes back over, unchanged."""
    bot_config = _make_bot_config(session, name="intraday-test", symbol="AAPL", risk_profile_name="CONSERVATIVE")
    provider = FakeMarketDataProvider({"AAPL": _bars_from_closes(FLAT_CLOSES)})
    bot = build_bot(session, bot_config, provider)
    bot.broker = FakeAsyncBroker(bot.portfolio)
    bot.strategy = AlwaysReturnStrategy(SignalAction.BUY, Decimal("100"))
    logger = logging.getLogger("test.daemon.pending_gate_filled")

    run_bot_cycle(bot, FakeClock(), logger)
    first_order = bot.broker.orders()[0]
    bot.broker.resolve_pending(first_order, fill_price=Decimal("100"))
    assert bot.portfolio.get_position("AAPL").quantity > 0

    run_bot_cycle(bot, FakeClock(), logger)

    assert len(bot.broker.orders()) == 1  # blocked by current_qty > 0, not the pending-order gate


def test_buy_is_allowed_again_once_the_pending_order_is_terminally_rejected(session):
    """A rejected/canceled/expired order must not permanently block the
    bot from ever entering - the gate only holds while the order is
    genuinely still open at the broker."""
    bot_config = _make_bot_config(session, name="intraday-test", symbol="AAPL", risk_profile_name="CONSERVATIVE")
    provider = FakeMarketDataProvider({"AAPL": _bars_from_closes(FLAT_CLOSES)})
    bot = build_bot(session, bot_config, provider)
    bot.broker = FakeAsyncBroker(bot.portfolio)
    bot.strategy = AlwaysReturnStrategy(SignalAction.BUY, Decimal("100"))
    logger = logging.getLogger("test.daemon.pending_gate_rejected")

    run_bot_cycle(bot, FakeClock(), logger)
    first_order = bot.broker.orders()[0]
    bot.broker.resolve_pending(first_order, rejected=True)

    run_bot_cycle(bot, FakeClock(), logger)

    orders = bot.broker.orders()
    assert len(orders) == 2  # the gate lifted - a fresh BUY was sent
    assert orders[0].status == OrderStatus.PENDING


def test_pending_buy_gate_is_a_structural_no_op_for_paperbroker(session):
    """Regression for the 4 existing swing bots: PaperBroker resolves
    submit_order() synchronously (FILLED or REJECTED before it returns),
    so no PENDING row for this bot/instrument can ever exist by the time
    the next signal is evaluated - the new gate must never fire for it,
    not because of a special case, but because its precondition is
    structurally unreachable.
    """
    bot_config = _make_bot_config(session, name="conservative-aapl", symbol="AAPL", risk_profile_name="CONSERVATIVE")
    provider = FakeMarketDataProvider({"AAPL": _bars_from_closes(UPTREND_CLOSES)})
    bot = build_bot(session, bot_config, provider)

    run_bot_cycle(bot, FakeClock(), logging.getLogger("test.daemon.pending_gate_paper"))

    instrument = bot.portfolio.get_or_create_instrument("AAPL")
    assert daemon_module._has_pending_order(bot, instrument, TradeSide.BUY) is False


# --- Stage 13 Part A follow-up: gate a second SELL on a still-pending one, --
# --- across all three SELL sources (signal / stop-loss / session-close) ----


def test_second_sell_is_not_sent_while_the_first_sell_order_is_still_pending(session, caplog):
    """Same class of bug as the BUY case, but for the strategy's own SELL
    signal: a stateless strategy re-issuing SELL on the next cycle must
    not stack a second closing order on top of an unconfirmed first one.
    """
    bot_config = _make_bot_config(session, name="intraday-test", symbol="AAPL", risk_profile_name="CONSERVATIVE")
    provider = FakeMarketDataProvider({"AAPL": _bars_from_closes(FLAT_CLOSES)})
    bot = build_bot(session, bot_config, provider)
    bot.broker = FakeAsyncBroker(bot.portfolio)
    bot.portfolio.buy("AAPL", Decimal("10"), Decimal("100"))  # pre-existing position, entry 100
    bot.strategy = AlwaysReturnStrategy(SignalAction.SELL, Decimal("100"))
    logger = logging.getLogger("test.daemon.pending_sell_gate")

    run_bot_cycle(bot, FakeClock(), logger)  # first cycle: submits the SELL
    assert len(bot.broker.orders()) == 1
    assert bot.broker.orders()[0].status == OrderStatus.PENDING
    assert bot.portfolio.get_position("AAPL").quantity == Decimal("10")  # still unconfirmed

    with caplog.at_level(logging.INFO, logger="test.daemon.pending_sell_gate"):
        run_bot_cycle(bot, FakeClock(), logger)  # second cycle: still pending, strategy says SELL again

    assert len(bot.broker.orders()) == 1  # no second order was sent
    assert "still pending at the broker" in caplog.text


def test_sell_is_gated_normally_once_the_pending_sell_fills(session):
    """Once sync_pending_orders() (simulated via resolve_pending()) confirms
    the fill, the position is really closed - the ordinary current_qty<=0
    'no open position' check takes back over, unchanged."""
    bot_config = _make_bot_config(session, name="intraday-test", symbol="AAPL", risk_profile_name="CONSERVATIVE")
    provider = FakeMarketDataProvider({"AAPL": _bars_from_closes(FLAT_CLOSES)})
    bot = build_bot(session, bot_config, provider)
    bot.broker = FakeAsyncBroker(bot.portfolio)
    bot.portfolio.buy("AAPL", Decimal("10"), Decimal("100"))
    bot.strategy = AlwaysReturnStrategy(SignalAction.SELL, Decimal("100"))
    logger = logging.getLogger("test.daemon.pending_sell_gate_filled")

    run_bot_cycle(bot, FakeClock(), logger)
    first_order = bot.broker.orders()[0]
    bot.broker.resolve_pending(first_order, fill_price=Decimal("100"))
    assert bot.portfolio.get_position("AAPL").quantity == Decimal("0")

    run_bot_cycle(bot, FakeClock(), logger)

    assert len(bot.broker.orders()) == 1  # blocked by current_qty<=0, not the pending-order gate


def test_sell_is_allowed_again_once_the_pending_sell_is_terminally_rejected(session):
    """A rejected SELL must not permanently block the bot from ever
    exiting - the gate only holds while the order is genuinely still open
    at the broker."""
    bot_config = _make_bot_config(session, name="intraday-test", symbol="AAPL", risk_profile_name="CONSERVATIVE")
    provider = FakeMarketDataProvider({"AAPL": _bars_from_closes(FLAT_CLOSES)})
    bot = build_bot(session, bot_config, provider)
    bot.broker = FakeAsyncBroker(bot.portfolio)
    bot.portfolio.buy("AAPL", Decimal("10"), Decimal("100"))
    bot.strategy = AlwaysReturnStrategy(SignalAction.SELL, Decimal("100"))
    logger = logging.getLogger("test.daemon.pending_sell_gate_rejected")

    run_bot_cycle(bot, FakeClock(), logger)
    first_order = bot.broker.orders()[0]
    bot.broker.resolve_pending(first_order, rejected=True)
    assert bot.portfolio.get_position("AAPL").quantity == Decimal("10")  # rejection never touched the position

    run_bot_cycle(bot, FakeClock(), logger)

    orders = bot.broker.orders()
    assert len(orders) == 2  # the gate lifted - a fresh SELL was sent
    assert orders[0].status == OrderStatus.PENDING


def test_force_session_close_is_gated_by_a_still_pending_stop_loss_sell(session):
    """Cross-source case: _check_stop_loss_take_profit submits a SELL
    (PENDING) one cycle; on a later cycle only _check_force_session_close's
    OWN condition is newly true (price is back to normal, so stop-loss no
    longer fires) - its SELL must still be blocked because the earlier
    stop-loss SELL for the same position hasn't resolved yet.
    """
    bot_config = _make_bot_config(
        session,
        name="intraday-test",
        symbol="AAPL",
        risk_profile_name="CONSERVATIVE",
        force_session_close=True,
    )
    drop_bars = _bars_from_closes([100] * 17 + [90])  # breaches CONSERVATIVE stop_loss_pct=0.025 (past 97.50)
    provider = FakeMarketDataProvider({"AAPL": drop_bars})
    bot = build_bot(session, bot_config, provider)
    bot.broker = FakeAsyncBroker(bot.portfolio)
    bot.portfolio.buy("AAPL", Decimal("10"), Decimal("100"))
    bot.strategy = AlwaysReturnStrategy(SignalAction.WAIT, Decimal("90"))

    early_clock = FakeClock(start_date=datetime(2024, 7, 15, 14, 0, tzinfo=timezone.utc))  # 10:00 ET
    logger = logging.getLogger("test.daemon.cross_source_gate")

    run_bot_cycle(bot, early_clock, logger)  # stop-loss fires -> SELL PENDING
    orders_after_first = bot.broker.orders()
    assert len(orders_after_first) == 1
    assert orders_after_first[0].status == OrderStatus.PENDING

    # Price back to normal (no longer breaching stop-loss) but Eastern time
    # has now moved past the force_session_close cutoff.
    provider.bars_by_symbol["AAPL"] = _bars_from_closes([100] * 18)
    late_clock = FakeClock(start_date=datetime(2024, 7, 15, 20, 0, tzinfo=timezone.utc))  # 16:00 ET
    run_bot_cycle(bot, late_clock, logger)

    assert len(bot.broker.orders()) == 1  # still just the stop-loss SELL - session-close did not stack another


def test_pending_sell_gate_is_a_structural_no_op_for_paperbroker(session):
    """Regression for the 4 existing swing bots (and the PaperBroker
    fallback path in general): PaperBroker resolves submit_order()
    synchronously, so no PENDING SELL row can ever exist by the time the
    next check runs - the gate's precondition is structurally unreachable,
    not specially bypassed."""
    bot_config = _make_bot_config(session, name="conservative-aapl", symbol="AAPL", risk_profile_name="CONSERVATIVE")
    provider = FakeMarketDataProvider({"AAPL": _bars_from_closes(FLAT_CLOSES)})
    bot = build_bot(session, bot_config, provider)

    instrument = bot.portfolio.get_or_create_instrument("AAPL")
    bot.broker.submit_order(bot.portfolio.account, instrument, TradeSide.BUY, Decimal("10"), price=Decimal("100"))
    bot.broker.submit_order(bot.portfolio.account, instrument, TradeSide.SELL, Decimal("10"), price=Decimal("101"))

    assert daemon_module._has_pending_order(bot, instrument, TradeSide.SELL) is False


# --- Stage 8b: position-level stop-loss/take-profit ------------------------


class AlwaysReturnStrategy(Strategy):
    """Test double: ignores the bars entirely and always returns the same
    Signal - used to prove the forced risk-exit fires independently of
    whatever the strategy says, per the Stage 8b requirement."""

    def __init__(self, action: SignalAction, price: Decimal):
        super().__init__({})
        self._action = action
        self._price = price

    def generate_signal(self, bars):
        return Signal(action=self._action, price=self._price)


def test_stop_loss_forces_a_full_exit_even_when_strategy_says_wait(session, caplog):
    bot_config = _make_bot_config(session, name="conservative-aapl", symbol="AAPL", risk_profile_name="CONSERVATIVE")
    drop_price = Decimal("90")  # entry 100, CONSERVATIVE stop_loss_pct=0.025 -> breached well past 97.50
    provider = FakeMarketDataProvider({"AAPL": _bars_from_closes([100] * 17 + [90])})
    bot = build_bot(session, bot_config, provider)

    # Open a position directly (bypassing the strategy) so the test controls
    # the entry price precisely.
    instrument = bot.portfolio.get_or_create_instrument("AAPL")
    bot.broker.submit_order(bot.portfolio.account, instrument, TradeSide.BUY, Decimal("10"), price=Decimal("100"))
    assert bot.portfolio.cash == Decimal("9000")

    # The strategy itself is indifferent (WAIT) - the forced exit must not
    # depend on or be gated by what generate_signal() returns.
    bot.strategy = AlwaysReturnStrategy(SignalAction.WAIT, drop_price)
    logger = logging.getLogger("test.daemon.stop_loss")

    with caplog.at_level(logging.INFO, logger="test.daemon.stop_loss"):
        run_bot_cycle(bot, FakeClock(), logger)

    position = bot.portfolio.get_position("AAPL")
    assert position.quantity == Decimal("0")
    assert bot.portfolio.cash == Decimal("9000") + Decimal("10") * drop_price
    assert "RISK_STOP_LOSS" in caplog.text
    assert "skipping strategy-driven execution" in caplog.text

    orders = bot.broker.orders()
    assert len(orders) == 2  # the manual entry buy + the forced exit sell
    assert all(o.status == OrderStatus.FILLED for o in orders)

    risk_signals = list(
        session.execute(select(SignalRecord).where(SignalRecord.action == SignalAction.RISK_STOP_LOSS)).scalars()
    )
    assert len(risk_signals) == 1
    assert risk_signals[0].price_at_signal == drop_price


def test_take_profit_also_forces_a_full_exit(session):
    bot_config = _make_bot_config(session, name="conservative-aapl", symbol="AAPL", risk_profile_name="CONSERVATIVE")
    rally_price = Decimal("120")  # entry 100, CONSERVATIVE take_profit_pct=0.05 -> breached well past 105
    provider = FakeMarketDataProvider({"AAPL": _bars_from_closes([100] * 17 + [120])})
    bot = build_bot(session, bot_config, provider)

    instrument = bot.portfolio.get_or_create_instrument("AAPL")
    bot.broker.submit_order(bot.portfolio.account, instrument, TradeSide.BUY, Decimal("10"), price=Decimal("100"))
    bot.strategy = AlwaysReturnStrategy(SignalAction.WAIT, rally_price)
    logger = logging.getLogger("test.daemon.take_profit")

    run_bot_cycle(bot, FakeClock(), logger)

    assert bot.portfolio.get_position("AAPL").quantity == Decimal("0")
    risk_signals = list(
        session.execute(select(SignalRecord).where(SignalRecord.action == SignalAction.RISK_TAKE_PROFIT)).scalars()
    )
    assert len(risk_signals) == 1


# --- Stage 13 Part C: force_session_close -----------------------------------


def test_force_session_close_closes_an_open_position_at_the_cutoff_regardless_of_signal(session, caplog):
    bot_config = _make_bot_config(
        session,
        name="intraday-test",
        symbol="AAPL",
        risk_profile_name="CONSERVATIVE",
        force_session_close=True,
    )
    provider = FakeMarketDataProvider({"AAPL": _bars_from_closes(FLAT_CLOSES)})
    bot = build_bot(session, bot_config, provider)

    instrument = bot.portfolio.get_or_create_instrument("AAPL")
    bot.broker.submit_order(bot.portfolio.account, instrument, TradeSide.BUY, Decimal("10"), price=Decimal("100"))
    assert bot.portfolio.cash == Decimal("9000")

    # The strategy itself is indifferent (WAIT) - the forced close must not
    # depend on or be gated by what generate_signal() returns. The close
    # price used for the forced exit is bars[-1].close (FLAT_CLOSES = 100),
    # not the strategy's own signal price - the strategy's price is only
    # ever used for its own (skipped) execution path.
    bot.strategy = AlwaysReturnStrategy(SignalAction.WAIT, Decimal("101"))
    # 2024-07-15 is EDT (UTC-4): 20:00 UTC = 16:00 ET, past the 15:55 cutoff.
    clock = FakeClock(start_date=datetime(2024, 7, 15, 20, 0, tzinfo=timezone.utc))
    logger = logging.getLogger("test.daemon.session_close")

    with caplog.at_level(logging.INFO, logger="test.daemon.session_close"):
        run_bot_cycle(bot, clock, logger)

    position = bot.portfolio.get_position("AAPL")
    assert position.quantity == Decimal("0")
    assert bot.portfolio.cash == Decimal("9000") + Decimal("10") * Decimal("100")
    assert "RISK_SESSION_CLOSE triggered" in caplog.text
    assert "skipping strategy-driven execution" in caplog.text

    risk_signals = list(
        session.execute(
            select(SignalRecord).where(SignalRecord.action == SignalAction.RISK_SESSION_CLOSE)
        ).scalars()
    )
    assert len(risk_signals) == 1


def test_force_session_close_before_the_cutoff_leaves_the_position_open(session):
    bot_config = _make_bot_config(
        session,
        name="intraday-test",
        symbol="AAPL",
        risk_profile_name="CONSERVATIVE",
        force_session_close=True,
    )
    provider = FakeMarketDataProvider({"AAPL": _bars_from_closes(FLAT_CLOSES)})
    bot = build_bot(session, bot_config, provider)

    instrument = bot.portfolio.get_or_create_instrument("AAPL")
    bot.broker.submit_order(bot.portfolio.account, instrument, TradeSide.BUY, Decimal("10"), price=Decimal("100"))
    bot.strategy = AlwaysReturnStrategy(SignalAction.WAIT, Decimal("101"))
    # 2024-07-15 14:00 UTC = 10:00 ET, well before the 15:55 cutoff.
    clock = FakeClock(start_date=datetime(2024, 7, 15, 14, 0, tzinfo=timezone.utc))

    run_bot_cycle(bot, clock, logging.getLogger("test.daemon.session_close_early"))

    assert bot.portfolio.get_position("AAPL").quantity == Decimal("10")  # untouched


def test_force_session_close_false_by_default_leaves_position_open_past_the_cutoff(session):
    """Regression: every existing bot (force_session_close=False, the
    default) must be completely unaffected by this check, even at a time
    that would trigger it for an opted-in bot."""
    bot_config = _make_bot_config(session, name="conservative-aapl", symbol="AAPL", risk_profile_name="CONSERVATIVE")
    assert bot_config.force_session_close is False

    provider = FakeMarketDataProvider({"AAPL": _bars_from_closes(FLAT_CLOSES)})
    bot = build_bot(session, bot_config, provider)

    instrument = bot.portfolio.get_or_create_instrument("AAPL")
    bot.broker.submit_order(bot.portfolio.account, instrument, TradeSide.BUY, Decimal("10"), price=Decimal("100"))
    bot.strategy = AlwaysReturnStrategy(SignalAction.WAIT, Decimal("101"))
    clock = FakeClock(start_date=datetime(2024, 7, 15, 20, 0, tzinfo=timezone.utc))  # 16:00 ET

    run_bot_cycle(bot, clock, logging.getLogger("test.daemon.session_close_off"))

    assert bot.portfolio.get_position("AAPL").quantity == Decimal("10")  # untouched


# --- Stage 8b: persisted circuit-breaker halt -------------------------------


def test_tripping_the_drawdown_circuit_breaker_persists_the_halt(session):
    bot_config = _make_bot_config(session, name="conservative-aapl", symbol="AAPL", risk_profile_name="CONSERVATIVE")
    provider = FakeMarketDataProvider({"AAPL": _bars_from_closes(FLAT_CLOSES)})
    bot = build_bot(session, bot_config, provider)

    # Force a drawdown well past CONSERVATIVE's max_drawdown_pct=0.09: peak
    # equity far above the bot's actual (flat, no-position) cash.
    bot.state.equity_peak = Decimal("20000")
    clock = FakeClock()

    _execute_signal(bot, Signal(action=SignalAction.BUY, price=Decimal("100")), clock, logging.getLogger("test.daemon.halt"))

    assert bot.state.halted_permanently is True
    assert bot.config.halted is True
    assert bot.config.halted_reason is not None
    assert bot.config.halted_at == clock.now()

    # Persisted, not just in-memory: a fresh read from the DB agrees.
    reloaded = session.get(BotConfig, bot_config.id)
    assert reloaded.halted is True


def test_load_enabled_bots_skips_a_halted_bot_after_a_simulated_restart(session):
    healthy_config = _make_bot_config(session, name="conservative-aapl", symbol="AAPL", risk_profile_name="CONSERVATIVE")
    halted_config = _make_bot_config(session, name="aggressive-tsla", symbol="TSLA", risk_profile_name="AGGRESSIVE")

    # Simulate a circuit breaker having tripped and been persisted in a
    # previous process run, then the daemon restarting.
    halted_config.halted = True
    halted_config.halted_reason = "Max drawdown circuit breaker tripped"
    halted_config.halted_at = datetime(2024, 6, 1, tzinfo=timezone.utc)
    session.commit()

    provider = FakeMarketDataProvider(
        {"AAPL": _bars_from_closes(FLAT_CLOSES), "TSLA": _bars_from_closes(FLAT_CLOSES)}
    )
    logger = logging.getLogger("test.daemon.restart_skip")
    bots = load_enabled_bots(session, provider, logger)

    assert [b.config.name for b in bots] == ["conservative-aapl"]

    # And the outer loop, run end-to-end, never even fetches bars for the
    # halted bot's symbol.
    run_daemon(
        session_factory=lambda: session,
        market_provider_factory=lambda s: provider,
        clock=FakeClock(),
        logger=logger,
        max_ticks=1,
    )
    assert "TSLA" not in provider.calls
    assert "AAPL" in provider.calls


# --- Stage 8b: stop-flag emergency stop -------------------------------------


class FlagBox:
    def __init__(self) -> None:
        self.value = False


class StopSignalingProvider(FakeMarketDataProvider):
    """Flips a shared flag as soon as a specific symbol is polled - used to
    simulate a stop-file appearing mid-tick, after bot A's cycle but before
    bot B's, within the same run_daemon tick."""

    def __init__(self, bars_by_symbol, flag: FlagBox, trigger_symbol: str):
        super().__init__(bars_by_symbol)
        self.flag = flag
        self.trigger_symbol = trigger_symbol

    def get_bars(self, symbol, start, end, timeframe="1Day"):
        bars = super().get_bars(symbol, start, end, timeframe)
        if symbol == self.trigger_symbol:
            self.flag.value = True
        return bars


def test_stop_flag_mid_tick_skips_remaining_bots_and_shuts_down_cleanly(session, caplog):
    # Insertion order matters: bot A (AAPL) must be polled before bot B
    # (TSLA) within a tick for this test to exercise the mid-tick check.
    _make_bot_config(session, name="bot-a", symbol="AAPL", risk_profile_name="CONSERVATIVE")
    _make_bot_config(session, name="bot-b", symbol="TSLA", risk_profile_name="AGGRESSIVE")

    flag = FlagBox()
    provider = StopSignalingProvider(
        {"AAPL": _bars_from_closes(FLAT_CLOSES), "TSLA": _bars_from_closes(FLAT_CLOSES)},
        flag=flag,
        trigger_symbol="AAPL",
    )
    logger = logging.getLogger("test.daemon.stop_flag")

    with caplog.at_level(logging.INFO, logger="test.daemon.stop_flag"):
        run_daemon(
            session_factory=lambda: session,
            market_provider_factory=lambda s: provider,
            clock=FakeClock(),
            logger=logger,
            shutdown_check=lambda: flag.value,
            tick_seconds=1,
        )

    # Bot A's in-flight cycle finished (never abandoned mid-order); bot B's
    # cycle never started because the flag was already set by the time its
    # turn came up in the same tick.
    assert provider.calls == ["AAPL"]
    assert "Stop requested mid-tick" in caplog.text
    assert "Daemon stopped." in caplog.text
