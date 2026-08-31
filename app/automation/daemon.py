from __future__ import annotations

import json
import logging
import sys
import time
from dataclasses import dataclass
from datetime import date, datetime, time as time_of_day, timedelta, timezone
from decimal import Decimal
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path
from typing import Callable

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.automation.lock import DaemonLock
from app.broker.alpaca_broker import AlpacaBroker
from app.broker.broker import Broker, BrokerError
from app.broker.paper_broker import PaperBroker
from app.core.paths import BASE_DIR, DATA_DIR, DB_PATH, ENV_PATH, LOGS_DIR
from app.database.models import Account, BotConfig, Instrument, Order, OrderStatus, SignalAction, TradeSide
from app.market.factory import build_market_provider
from app.market.provider import MarketDataError, MarketDataProvider
from app.market.session import to_eastern
from app.portfolio.portfolio import Portfolio
from app.risk.risk_manager import PortfolioState, RiskManager
from app.risk.risk_profile import RiskProfile, get_risk_profile
from app.strategies.registry import get_strategy_class
from app.strategies.runner import evaluate_and_log, log_risk_signal
from app.strategies.strategy import Signal, Strategy

# Eastern local time at/after which a force_session_close=True bot's open
# position gets closed regardless of its strategy's signal - 5 minutes
# before NYSE's 16:00 ET close, so the market order has room to fill
# before the session actually ends. Only bots that opt in via
# BotConfig.force_session_close are affected; every existing bot defaults
# to False (see _check_force_session_close).
SESSION_CLOSE_CUTOFF_ET = time_of_day(15, 55)

# The documented, primary way to stop the daemon (Stage 8b): create this
# file (e.g. `New-Item data\daemon.stop` on Windows, `touch data/daemon.stop`
# on Linux/systemd - see Stage 9a) and the daemon exits after finishing
# whatever bot cycle is in flight, then removes the file so a stale flag
# doesn't block the next start. Windows only delivers a catchable Ctrl+C to
# processes sharing the launching console, which made SIGINT unreliable to
# trigger from tooling/scripts - this file check works regardless of how or
# on which OS the daemon was started (SIGTERM, the systemd-native stop
# signal on Linux, is still handled below too - see main()).
STOP_FILE_NAME = "daemon.stop"

# How far back to look for history each poll, per timeframe unit - keyed
# by the same Min/Hour/Day/Week/Month suffix convention the market data
# provider's own timeframe parsing uses. "Day" (400 days, ~1.5y of daily
# bars) matches the UI's own "Проверить сигнал" window (main.py) and is exactly what
# every bot in production uses today - Week/Month share it since a single
# bar there also represents a whole calendar day or more, same as "Day".
# Minute/Hour values are deliberately conservative placeholders, not a
# calibrated choice - real intraday strategies need their own tuning (a
# separate task), not the daily-bar window, which would be either wildly
# excessive (400 days of minute bars) or accidentally too short.
DEFAULT_HISTORY_LOOKBACK_DAYS = 400
HISTORY_LOOKBACK_DAYS_BY_TIMEFRAME_UNIT: dict[str, int] = {
    "Day": DEFAULT_HISTORY_LOOKBACK_DAYS,
    "Week": DEFAULT_HISTORY_LOOKBACK_DAYS,
    "Month": DEFAULT_HISTORY_LOOKBACK_DAYS,
    "Hour": 30,
    "Min": 5,
}


def _history_lookback_days(timeframe: str) -> int:
    for unit, days in HISTORY_LOOKBACK_DAYS_BY_TIMEFRAME_UNIT.items():
        if timeframe.endswith(unit):
            return days
    return DEFAULT_HISTORY_LOOKBACK_DAYS

# How often the outer loop wakes to check whether any bot is due for its
# own poll_interval_seconds - independent of any individual bot's cadence.
DEFAULT_TICK_SECONDS = 1.0


class Clock:
    """Thin wrapper around time/datetime so tests can inject a fake one -
    the daemon must never have to literally sleep for a bot's real
    poll_interval_seconds (or wait for a real day boundary) in a test."""

    def now(self) -> datetime:
        return datetime.now(timezone.utc)

    def monotonic(self) -> float:
        return time.monotonic()

    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)


def stop_file_path() -> Path:
    return DATA_DIR / STOP_FILE_NAME


@dataclass
class BotRuntimeState:
    """In-memory-only bookkeeping for one bot, for the lifetime of this
    daemon process.

    equity_peak/day_start_equity/halted_for_today are intentionally not
    persisted - they reset naturally each day or process restart, which is
    an acceptable simplification for those. halted_permanently mirrors
    BotConfig.halted (see build_bot()/_execute_signal()) so a drawdown
    circuit-breaker trip survives a daemon restart instead of silently
    resetting - that one *is* persisted, in the database, not here.
    """

    last_equity: Decimal
    equity_peak: Decimal
    day_start_equity: Decimal
    current_day: date | None = None
    halted_for_today: bool = False
    halted_permanently: bool = False


@dataclass
class Bot:
    config: BotConfig
    portfolio: Portfolio
    strategy: Strategy
    risk_profile: RiskProfile
    broker: Broker
    market_provider: MarketDataProvider | None
    state: BotRuntimeState
    next_poll_at: float = 0.0  # monotonic seconds; 0 -> due immediately


def build_bot(
    session: Session, bot_config: BotConfig, market_provider: MarketDataProvider | None
) -> Bot:
    """Wires up one bot's Strategy/RiskProfile/Portfolio/Broker purely from
    existing classes - reimplements none of their logic.

    PAPER-ONLY INVARIANT: this is the daemon's one and only place that
    constructs a Broker, and BotConfig.broker only ever selects *which*
    broker - PaperBroker (default "paper", fully internal) or AlpacaBroker
    ("alpaca_paper", real orders to Alpaca's paper endpoint) - never
    whether it trades live. AlpacaBroker(portfolio) is called here with no
    `paper` argument of any kind; the paper-trading flag is hardcoded true
    inside AlpacaBroker itself (see its docstring) and is not read from
    this config, an env var, or anywhere else reachable from this module -
    so there is no path from here to a live TradingClient. (Historically
    this docstring said daemon.py never even imports AlpacaBroker at all;
    that changed deliberately once broker selection became per-bot - see
    test_daemon_module_never_passes_a_paper_argument_to_a_broker for the
    invariant that actually matters now.)
    """
    account = session.get(Account, bot_config.account_id)
    if account is None:
        raise ValueError(
            f"BotConfig {bot_config.name!r} references missing account_id={bot_config.account_id}"
        )
    portfolio = Portfolio(session, account)

    strategy_cls = get_strategy_class(bot_config.strategy_class)
    strategy_params = json.loads(bot_config.strategy_params_json)
    strategy = strategy_cls(strategy_params)

    risk_profile = get_risk_profile(bot_config.risk_profile_name)

    broker: Broker
    if bot_config.broker == "alpaca_paper":
        broker = AlpacaBroker(portfolio)  # no `paper` argument - see docstring above
    else:
        broker = PaperBroker(portfolio, market_provider)

    initial_equity = account.cash
    state = BotRuntimeState(
        last_equity=initial_equity,
        equity_peak=initial_equity,
        day_start_equity=initial_equity,
    )

    return Bot(
        config=bot_config,
        portfolio=portfolio,
        strategy=strategy,
        risk_profile=risk_profile,
        broker=broker,
        market_provider=market_provider,
        state=state,
    )


def load_enabled_bots(
    session: Session, market_provider: MarketDataProvider | None, logger: logging.Logger
) -> list[Bot]:
    """Loads every enabled bot - except ones halted by a tripped circuit
    breaker, which are skipped entirely (not even constructed) both on
    startup and for the rest of this process's run, until someone clears
    the halt (see app.automation.manage_bots.clear_halt).
    """
    configs = list(session.execute(select(BotConfig).where(BotConfig.enabled.is_(True))).scalars())
    bots = []
    for config in configs:
        if config.halted:
            logger.warning(
                "[%s] skipping halted bot (reason=%s, halted_at=%s) - "
                "clear the halt with app.automation.manage_bots.clear_halt to resume",
                config.name,
                config.halted_reason,
                config.halted_at,
            )
            continue
        bots.append(build_bot(session, config, market_provider))
    return bots


# Tolerance for _check_alpaca_capital_consistency's comparison - purely to
# avoid a false-positive warning from Decimal rounding noise, not because
# any real divergence smaller than this is expected or acceptable.
ALPACA_CAPITAL_CONSISTENCY_TOLERANCE = Decimal("0.01")


def _check_alpaca_capital_consistency(bots: list[Bot], logger: logging.Logger) -> None:
    """KNOWN LIMITATION (Stage 14 - see the DEFAULT_BOT_SPECS docstring in
    app.automation.setup_bots): every alpaca_paper bot still keeps its own
    independent local Account ledger, even though - unlike PaperBroker
    bots, where each bot's local ledger really is its own isolated paper
    account - every alpaca_paper bot actually submits real orders to the
    same *shared* Alpaca paper account. That makes "sum of local
    Account.cash across alpaca_paper bots" and "Alpaca's real cash
    balance" two independent, legitimately divergent sources of truth for
    how much capital exists - this function does not reconcile them, only
    makes the divergence loud (a WARNING at every daemon startup) instead
    of silent. Proper shared-capital accounting belongs to
    RiskProfile.capital_allocation_pct enforcement (see its docstring),
    part of the Paper -> Live promotion criteria, not this stage.

    Never raises and never blocks daemon startup: a network/auth failure
    while fetching Alpaca's real balance is logged as its own WARNING
    (distinct from an actual divergence, so the two cases are never
    conflated) and this function simply returns.
    """
    alpaca_bots = [b for b in bots if b.config.broker == "alpaca_paper"]
    if not alpaca_bots:
        return

    local_total = sum((b.portfolio.cash for b in alpaca_bots), Decimal("0"))

    try:
        real_cash = alpaca_bots[0].broker.get_account_cash()
    except BrokerError as exc:
        logger.warning(
            "Could not verify local/Alpaca capital consistency for %d alpaca_paper "
            "bot(s) (%s) - failed to fetch the real Alpaca paper account balance: %s. "
            "Skipping this check for this startup.",
            len(alpaca_bots),
            ", ".join(b.config.name for b in alpaca_bots),
            exc,
        )
        return

    if abs(local_total - real_cash) > ALPACA_CAPITAL_CONSISTENCY_TOLERANCE:
        logger.warning(
            "Local capital ledger diverges from Alpaca's real paper account balance: "
            "%d alpaca_paper bot(s) (%s) sum to a local cash total of %s, but the shared "
            "Alpaca paper account actually holds %s in cash. This is a known, expected "
            "architectural limitation, not a bug - each bot's local Account is independent "
            "bookkeeping inherited from before broker=\"alpaca_paper\" existed, not a claim "
            "on a slice of the real balance, and RiskProfile.capital_allocation_pct is not "
            "enforced across bots (see app.automation.setup_bots.DEFAULT_BOT_SPECS and "
            "app.risk.risk_profile.RiskProfile.capital_allocation_pct docstrings).",
            len(alpaca_bots),
            ", ".join(b.config.name for b in alpaca_bots),
            local_total,
            real_cash,
        )


def _update_day_boundary(state: BotRuntimeState, today: date) -> None:
    if state.current_day is None:
        state.current_day = today
    elif today != state.current_day:
        state.day_start_equity = state.last_equity
        state.halted_for_today = False
        state.current_day = today


def _mark_equity(bot: Bot, current_qty: Decimal, price: Decimal) -> Decimal:
    equity = bot.portfolio.cash + current_qty * price
    bot.state.last_equity = equity
    if equity > bot.state.equity_peak:
        bot.state.equity_peak = equity
    return equity


def _check_stop_loss_take_profit(bot: Bot, current_price: Decimal, logger: logging.Logger) -> bool:
    """Position-level protective exit, independent of the strategy's own
    signal this cycle: if current_price has breached the open position's
    stop-loss or take-profit threshold (from the bot's RiskProfile), force
    a full exit regardless of what generate_signal() says.

    Deliberately bypasses RiskManager.evaluate() entirely - that method
    sizes and gates *new* entries, but this is closing an existing
    position outright, not a new decision to size. Logged via
    log_risk_signal() with RISK_STOP_LOSS/RISK_TAKE_PROFIT so it's always
    distinguishable from a strategy-driven exit in the Signal/Trade
    history and in logs/daemon.log.

    Returns True if a forced exit was executed this cycle (so the caller
    knows to skip the strategy's own signal for this cycle).
    """
    name = bot.config.name
    position = bot.portfolio.get_position(bot.config.symbol)
    if position is None or position.quantity <= 0:
        return False

    entry_price = position.avg_price
    if entry_price <= 0:
        return False

    stop_loss_price = entry_price * (Decimal("1") - Decimal(str(bot.risk_profile.stop_loss_pct)))
    take_profit_price = entry_price * (Decimal("1") + Decimal(str(bot.risk_profile.take_profit_pct)))

    if current_price <= stop_loss_price:
        action = SignalAction.RISK_STOP_LOSS
    elif current_price >= take_profit_price:
        action = SignalAction.RISK_TAKE_PROFIT
    else:
        return False

    quantity = position.quantity
    instrument = bot.portfolio.get_or_create_instrument(bot.config.symbol)

    if _has_pending_order(bot, instrument, TradeSide.SELL):
        logger.info(
            "[%s] %s condition met but a SELL order is already pending at the broker - "
            "no-op (avoiding a duplicate close)",
            name,
            action.value,
        )
        return False

    log_risk_signal(
        bot.portfolio.session,
        instrument,
        action,
        current_price,
        reason=f"entry_price={entry_price} trigger_price={current_price}",
    )

    order = bot.broker.submit_order(bot.portfolio.account, instrument, TradeSide.SELL, quantity, price=current_price)
    logger.warning(
        "[%s] %s triggered: closed %s @ %s (entry was %s) order_id=%s",
        name,
        action.value,
        quantity,
        current_price,
        entry_price,
        order.id,
    )
    return True


def _check_force_session_close(bot: Bot, current_price: Decimal, clock: Clock, logger: logging.Logger) -> bool:
    """Unconditionally closes an open position once Eastern local time
    reaches SESSION_CLOSE_CUTOFF_ET, independent of the strategy's own
    signal this cycle - only for bots with BotConfig.force_session_close=
    True (an intraday bot that must never hold overnight). Every existing
    bot defaults to False and is completely untouched by this check.

    Mirrors _check_stop_loss_take_profit's shape deliberately: bypasses
    RiskManager.evaluate() for the same reason (this closes an existing
    position outright, it isn't a new entry to size), and is logged via
    log_risk_signal() with RISK_SESSION_CLOSE so it's always
    distinguishable from a strategy-driven exit or a stop-loss/take-profit
    one in the Signal/Trade history.

    Returns True if a forced exit was executed this cycle (so the caller
    knows to skip the strategy's own signal for this cycle).
    """
    if not bot.config.force_session_close:
        return False

    position = bot.portfolio.get_position(bot.config.symbol)
    if position is None or position.quantity <= 0:
        return False

    if to_eastern(clock.now()).time() < SESSION_CLOSE_CUTOFF_ET:
        return False

    name = bot.config.name
    quantity = position.quantity
    instrument = bot.portfolio.get_or_create_instrument(bot.config.symbol)

    if _has_pending_order(bot, instrument, TradeSide.SELL):
        logger.info(
            "[%s] force_session_close condition met but a SELL order is already pending "
            "at the broker - no-op (avoiding a duplicate close)",
            name,
        )
        return False

    log_risk_signal(
        bot.portfolio.session,
        instrument,
        SignalAction.RISK_SESSION_CLOSE,
        current_price,
        reason=f"force_session_close: Eastern time reached {SESSION_CLOSE_CUTOFF_ET}",
    )

    order = bot.broker.submit_order(bot.portfolio.account, instrument, TradeSide.SELL, quantity, price=current_price)
    logger.warning(
        "[%s] %s triggered (Eastern time >= %s): closed %s @ %s order_id=%s",
        name,
        SignalAction.RISK_SESSION_CLOSE.value,
        SESSION_CLOSE_CUTOFF_ET,
        quantity,
        current_price,
        order.id,
    )
    return True


def _has_pending_order(bot: Bot, instrument: Instrument, side: TradeSide) -> bool:
    """True if this bot's account already has a locally-PENDING order of
    this side for this instrument - i.e. submitted to the broker but not
    yet resolved (FILLED or REJECTED) by sync_pending_orders().

    Needed alongside current_qty-based checks, not instead of them: for an
    asynchronous broker (AlpacaBroker), submit_order() is fire-and-forget
    and never touches Portfolio - a position only changes once
    sync_pending_orders() sees the order FILLED - so current_qty alone
    stays stale for as long as an order is still pending at the broker.

    Applied to BUY (a stateless strategy re-issuing BUY on the next cycle
    would otherwise stack a second entry on an unconfirmed first one) and,
    more importantly, to SELL: three independent call sites can each
    submit a closing SELL for the same position (the strategy's own
    signal in _execute_signal, _check_stop_loss_take_profit,
    _check_force_session_close) - without this gate, two of them could
    fire before the first SELL resolves and each submit a full-quantity
    SELL. AlpacaBroker sends a bare side=SELL market order with no
    server-side "sell to close" guard (see its docstring) - a second SELL
    stacked on an unconfirmed first one could sell more than the actual
    position and open an unintended short, which no strategy in this
    project is designed to do (every one is long-only).

    For PaperBroker this is a structural no-op, not a special-cased
    exemption: submit_order() resolves synchronously (FILLED or REJECTED)
    inside the same call, so no PENDING row for this bot/instrument/side
    can exist by the time the next check runs.
    """
    return (
        bot.portfolio.session.execute(
            select(Order.id)
            .where(
                Order.account_id == bot.portfolio.account.id,
                Order.instrument_id == instrument.id,
                Order.side == side,
                Order.status == OrderStatus.PENDING,
            )
            .limit(1)
        ).scalar_one_or_none()
        is not None
    )


def _execute_signal(bot: Bot, signal: Signal, clock: Clock, logger: logging.Logger) -> None:
    name = bot.config.name
    price = signal.price
    if price is None:
        logger.warning(
            "[%s] %s signal has no price attached - skipping execution", name, signal.action.value
        )
        return

    instrument = bot.portfolio.get_or_create_instrument(bot.config.symbol)
    position = bot.portfolio.get_position(bot.config.symbol)
    current_qty = position.quantity if position is not None else Decimal("0")
    equity = _mark_equity(bot, current_qty, price)

    if signal.action == SignalAction.SELL and current_qty <= 0:
        logger.info("[%s] SELL signal but no open position - no-op", name)
        return
    if signal.action == SignalAction.BUY and current_qty > 0:
        logger.info("[%s] BUY signal but already holding a position - no-op (no pyramiding)", name)
        return
    if signal.action == SignalAction.BUY and _has_pending_order(bot, instrument, TradeSide.BUY):
        logger.info(
            "[%s] BUY signal but a previous BUY order is still pending at the broker - "
            "no-op (avoiding a duplicate order)",
            name,
        )
        return
    if signal.action == SignalAction.SELL and _has_pending_order(bot, instrument, TradeSide.SELL):
        logger.info(
            "[%s] SELL signal but a previous SELL order is still pending at the broker - "
            "no-op (avoiding a duplicate order)",
            name,
        )
        return

    portfolio_state = PortfolioState(
        cash=bot.portfolio.cash,
        position_qty=current_qty,
        equity_peak=bot.state.equity_peak,
        current_equity=equity,
        day_start_equity=bot.state.day_start_equity,
        halted=bot.state.halted_permanently or bot.state.halted_for_today,
    )
    decision = RiskManager().evaluate(signal.action, price, portfolio_state, bot.risk_profile)

    if decision.halt_for_day:
        bot.state.halted_for_today = True
        logger.warning(
            "[%s] daily loss limit breached - halting new entries for the rest of the day", name
        )
    if decision.halt_permanently:
        bot.state.halted_permanently = True
        bot.config.halted = True
        bot.config.halted_reason = decision.reason
        bot.config.halted_at = clock.now()
        bot.portfolio.session.commit()
        logger.warning(
            "[%s] drawdown circuit breaker tripped - halted until manually cleared (persisted)", name
        )

    if not decision.approved:
        logger.info("[%s] %s rejected by risk manager: %s", name, signal.action.value, decision.reason)
        return

    quantity = current_qty if signal.action == SignalAction.SELL else decision.quantity
    side = TradeSide.BUY if signal.action == SignalAction.BUY else TradeSide.SELL
    order = bot.broker.submit_order(bot.portfolio.account, instrument, side, quantity, price=price)
    logger.info(
        "[%s] %s filled: qty=%s price=%s order_id=%s",
        name,
        signal.action.value,
        quantity,
        price,
        order.id,
    )


def _check_stray_pending_orders(bot: Bot, logger: logging.Logger) -> None:
    """PaperBroker fills synchronously, so nothing should ever stay
    PENDING under it - this is a defensive 'sync' pass confirming that
    invariant and loudly logging if it's ever violated (which would
    indicate a bug). Not meaningful for AlpacaBroker, which is
    legitimately asynchronous - a PENDING order there just means Alpaca
    hasn't filled it yet, reconciled next cycle by sync_pending_orders() -
    so this check only runs for PaperBroker bots.
    """
    if not isinstance(bot.broker, PaperBroker):
        return
    pending = [o for o in bot.broker.orders() if o.status == OrderStatus.PENDING]
    if pending:
        logger.warning(
            "[%s] found %d unexpectedly PENDING order(s) - PaperBroker should always fill synchronously",
            bot.config.name,
            len(pending),
        )


def run_bot_cycle(bot: Bot, clock: Clock, logger: logging.Logger) -> None:
    """One poll: fetch bars, evaluate the strategy, log the Signal (via the
    existing evaluate_and_log(), never reimplemented here), and - if
    actionable and risk-approved - execute it. This is the unit the tests
    exercise directly, without needing the outer scheduling loop.
    """
    name = bot.config.name
    _update_day_boundary(bot.state, clock.now().date())

    try:
        bot.broker.sync_pending_orders()
    except BrokerError as exc:
        # PaperBroker's sync_pending_orders() is the inherited no-op
        # default (see app.broker.broker.Broker) and never raises -
        # this only matters for AlpacaBroker, where a network/auth
        # failure here means position state may be stale, so skip the
        # rest of this cycle rather than act on possibly-outdated data.
        logger.warning("[%s] failed to sync pending orders: %s", name, exc)
        return

    if bot.market_provider is None:
        logger.warning("[%s] no market data provider configured - skipping poll", name)
        return

    timeframe = bot.config.timeframe
    end = clock.now().date()
    start = end - timedelta(days=_history_lookback_days(timeframe))
    try:
        bars = bot.market_provider.get_bars(bot.config.symbol, start, end, timeframe=timeframe)
    except MarketDataError as exc:
        logger.warning("[%s] failed to fetch bars for %s: %s", name, bot.config.symbol, exc)
        return

    if not bars:
        logger.info("[%s] no bars available for %s - skipping", name, bot.config.symbol)
        return

    current_price = bars[-1].close
    forced_exit = _check_stop_loss_take_profit(bot, current_price, logger)
    if not forced_exit:
        forced_exit = _check_force_session_close(bot, current_price, clock, logger)

    instrument = bot.portfolio.get_or_create_instrument(bot.config.symbol)
    signal = evaluate_and_log(bot.portfolio.session, bot.strategy, instrument, bars)
    logger.info("[%s] signal=%s price=%s", name, signal.action.value, signal.price)

    if forced_exit:
        logger.info(
            "[%s] skipping strategy-driven execution this cycle - "
            "position was just force-closed by risk management",
            name,
        )
    elif signal.action == SignalAction.WAIT:
        logger.info("[%s] WAIT - no action taken", name)
    else:
        _execute_signal(bot, signal, clock, logger)

    _check_stray_pending_orders(bot, logger)


def run_daemon(
    session_factory: Callable[[], Session],
    market_provider_factory: Callable[[Session], MarketDataProvider | None],
    clock: Clock | None = None,
    logger: logging.Logger | None = None,
    shutdown_check: Callable[[], bool] | None = None,
    tick_seconds: float = DEFAULT_TICK_SECONDS,
    max_ticks: int | None = None,
) -> None:
    """The outer scheduling loop.

    Deliberately simple sequential polling with per-bot cadence tracking,
    not asyncio/threads: with a handful of bots and network-bound,
    once-per-poll work, real concurrency would mean thread-safety around a
    shared SQLAlchemy Session (or a session-per-thread rewrite) and async
    variants of every existing class, for no measurable benefit at this
    scale. The loop just checks, every tick_seconds, which bots are due
    and runs them one at a time.

    max_ticks is test-only: run a bounded number of ticks instead of
    forever, so tests never depend on wall-clock time or Ctrl+C.

    shutdown_check is polled both at the top of this loop (between ticks)
    and again before each individual bot's cycle within a tick, so a stop
    request that appears mid-tick still skips any bot that hasn't started
    yet - without ever abandoning a cycle already in progress, since
    run_bot_cycle() itself is never interrupted partway through.
    """
    clock = clock or Clock()
    logger = logger or logging.getLogger("mizan.daemon")
    shutdown_check = shutdown_check or (lambda: False)

    session = session_factory()
    try:
        market_provider = market_provider_factory(session)
        bots = load_enabled_bots(session, market_provider, logger)
        if not bots:
            logger.warning("No enabled, non-halted bots found - nothing to do")
            return

        logger.info("Loaded %d bot(s): %s", len(bots), ", ".join(b.config.name for b in bots))
        _check_alpaca_capital_consistency(bots, logger)

        ticks = 0
        while not shutdown_check():
            now = clock.monotonic()
            for bot in bots:
                if shutdown_check():
                    logger.info("Stop requested mid-tick - skipping remaining bots this cycle")
                    break
                if now >= bot.next_poll_at:
                    try:
                        run_bot_cycle(bot, clock, logger)
                    except Exception:
                        logger.exception(
                            "[%s] unexpected error during poll - continuing", bot.config.name
                        )
                    bot.next_poll_at = clock.monotonic() + bot.config.poll_interval_seconds

            ticks += 1
            if max_ticks is not None and ticks >= max_ticks:
                break
            if shutdown_check():
                break
            clock.sleep(tick_seconds)
    finally:
        session.close()
        logger.info("Daemon stopped.")


# Stage 14 (post .vscode-server disk-full incident): logs/daemon.log was a
# plain FileHandler - unbounded, growing forever. That specific incident's
# actual cause was unrelated (VS Code Remote-SSH server cache), but an
# ever-growing log file is a real, separate way to eventually fill the
# same disk, so it's closed here too rather than left as a known gap.
# 14 days keeps roughly two weeks of history (observed growth is well
# under 1MB/day for 5 bots - see logs/daemon.log's actual size on the VPS
# - so 14 backups is comfortably small, not a real space concern even on
# the VPS's 6.7GB volume) while still bounding it permanently instead of
# relying on someone remembering to clean it up by hand.
DAEMON_LOG_ROTATION_BACKUP_DAYS = 14


def build_daemon_logger() -> logging.Logger:
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("mizan.daemon")
    logger.setLevel(logging.INFO)
    if not logger.handlers:  # avoid duplicate handlers if this is called more than once
        formatter = logging.Formatter(
            fmt="%(asctime)s [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
        )
        # TimedRotatingFileHandler, not plain FileHandler: rotates
        # logs/daemon.log at midnight (host-local time - same clock the
        # %(asctime)s timestamps inside each line already use, so the
        # rotated filename's date and the lines' own timestamps never
        # disagree) and keeps only the last DAEMON_LOG_ROTATION_BACKUP_DAYS
        # rotated files, deleting older ones automatically. Line format is
        # unchanged - app.automation.log_notifier parses that format by
        # regex and must keep working against it either way.
        file_handler = TimedRotatingFileHandler(
            LOGS_DIR / "daemon.log",
            when="midnight",
            backupCount=DAEMON_LOG_ROTATION_BACKUP_DAYS,
            encoding="utf-8",
        )
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)

        # MizanDaemon.exe (Stage 8c) is built with console=False so Task
        # Scheduler can run it with nobody logged in and no desktop session
        # at all - in that mode sys.stderr is None, and logging.StreamHandler()
        # defaults to stderr, so attaching one unconditionally would raise
        # AttributeError on the first log call. logs/daemon.log (above) is
        # the only sink guaranteed to work in every launch mode; the console
        # handler is just a nice-to-have for `python daemon.py` in a real
        # terminal.
        if sys.stderr is not None:
            console_handler = logging.StreamHandler()
            console_handler.setFormatter(formatter)
            logger.addHandler(console_handler)
    return logger


def main() -> None:
    import signal as signal_module

    from app.database.session import get_engine, get_session_factory

    logger = build_daemon_logger()

    lock = DaemonLock()
    if not lock.acquire(logger):
        return

    shutdown_requested = False
    stop_file = stop_file_path()
    stop_file_logged = False

    def _handle_shutdown(signum, frame):
        nonlocal shutdown_requested
        logger.info("Shutdown signal received (%s) - finishing current bot cycle, then stopping", signum)
        shutdown_requested = True

    # SIGINT/SIGTERM are kept as a secondary path, but Windows only ever
    # delivers a catchable Ctrl+C to a process sharing the sender's
    # console, which makes it unreliable to trigger from scripts/tooling -
    # the stop-file check below is the primary, documented way to stop.
    signal_module.signal(signal_module.SIGINT, _handle_shutdown)
    signal_module.signal(signal_module.SIGTERM, _handle_shutdown)

    def _shutdown_requested() -> bool:
        nonlocal stop_file_logged
        if shutdown_requested:
            return True
        if stop_file.exists():
            if not stop_file_logged:
                logger.info(
                    "Stop flag file detected (%s) - shutting down after the in-flight cycle completes",
                    stop_file,
                )
                stop_file_logged = True
            return True
        return False

    if sys.platform == "win32":
        stop_hint = f"New-Item -ItemType File -Force {stop_file} (PowerShell)"
    else:
        stop_hint = f"touch {stop_file}"

    logger.info("Mizan automation daemon starting - PAPER TRADING ONLY")
    logger.info(
        "Paths: BASE_DIR=%s .env=%s (exists=%s) db=%s (exists=%s)",
        BASE_DIR,
        ENV_PATH,
        ENV_PATH.exists(),
        DB_PATH,
        DB_PATH.exists(),
    )
    logger.info("To stop: create %s (e.g. %s), or Ctrl+C/SIGTERM", stop_file, stop_hint)

    engine = get_engine()
    session_factory = get_session_factory(engine)

    try:
        run_daemon(
            session_factory,
            build_market_provider,
            shutdown_check=_shutdown_requested,
        )

        if stop_file.exists():
            stop_file.unlink()
            logger.info("Removed stop flag file %s so it won't block the next start", stop_file)
    finally:
        # Runs on every exit path out of run_daemon - stop-flag, SIGINT/
        # SIGTERM, or an unhandled exception - so the lock never outlives
        # this process on any graceful (or semi-graceful) shutdown. Only an
        # abrupt external kill (power loss, TerminateProcess) skips this,
        # which is exactly the "stale lock" case DaemonLock.acquire() above
        # already knows how to recover from on the next start.
        lock.release()
        logger.info("Released single-instance lock.")


if __name__ == "__main__":
    main()
