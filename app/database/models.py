from __future__ import annotations

import enum
from datetime import date, datetime, timezone
from decimal import Decimal

from sqlalchemy import Date, DateTime, Enum, ForeignKey, Numeric, String, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

# Fixed-point money/quantity storage: enough precision for USD prices and
# fractional shares while staying exact (avoids binary-float rounding drift).
MONEY = Numeric(18, 6, asdecimal=True)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class TradeSide(str, enum.Enum):
    BUY = "BUY"
    SELL = "SELL"


class OrderStatus(str, enum.Enum):
    PENDING = "PENDING"
    FILLED = "FILLED"
    REJECTED = "REJECTED"


class SignalAction(str, enum.Enum):
    BUY = "BUY"
    SELL = "SELL"
    WAIT = "WAIT"
    # Forced position exits from a RiskProfile's stop_loss_pct/take_profit_pct
    # breach - never emitted by Strategy.generate_signal(), only by the
    # automation daemon's own position monitoring (app.automation.daemon),
    # so they're always distinguishable in the Signal/Trade history from a
    # strategy-driven BUY/SELL.
    RISK_STOP_LOSS = "RISK_STOP_LOSS"
    RISK_TAKE_PROFIT = "RISK_TAKE_PROFIT"
    # Forced exit from BotConfig.force_session_close, independent of both
    # the strategy's signal and RiskProfile's stop-loss/take-profit -
    # triggered purely by time of day (see app.automation.daemon), only
    # for bots that opt in.
    RISK_SESSION_CLOSE = "RISK_SESSION_CLOSE"


class Account(Base):
    __tablename__ = "accounts"

    id: Mapped[int] = mapped_column(primary_key=True)
    cash: Mapped[Decimal] = mapped_column(MONEY, nullable=False, default=Decimal("0"))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, nullable=False)

    positions: Mapped[list["Position"]] = relationship(back_populates="account")
    trades: Mapped[list["Trade"]] = relationship(back_populates="account")
    orders: Mapped[list["Order"]] = relationship(back_populates="account")
    bot_configs: Mapped[list["BotConfig"]] = relationship(back_populates="account")


class Instrument(Base):
    __tablename__ = "instruments"

    id: Mapped[int] = mapped_column(primary_key=True)
    symbol: Mapped[str] = mapped_column(String(32), unique=True, nullable=False, index=True)
    name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    asset_class: Mapped[str] = mapped_column(String(32), nullable=False, default="US_EQUITY")

    positions: Mapped[list["Position"]] = relationship(back_populates="instrument")
    trades: Mapped[list["Trade"]] = relationship(back_populates="instrument")
    orders: Mapped[list["Order"]] = relationship(back_populates="instrument")
    price_bars: Mapped[list["PriceBar"]] = relationship(back_populates="instrument")
    cached_days: Mapped[list["CachedDay"]] = relationship(back_populates="instrument")
    signals: Mapped[list["Signal"]] = relationship(back_populates="instrument")
    backtest_runs: Mapped[list["BacktestRun"]] = relationship(back_populates="instrument")


class Position(Base):
    __tablename__ = "positions"
    __table_args__ = (
        UniqueConstraint("account_id", "instrument_id", name="uq_position_account_instrument"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id"), nullable=False)
    instrument_id: Mapped[int] = mapped_column(ForeignKey("instruments.id"), nullable=False)
    quantity: Mapped[Decimal] = mapped_column(MONEY, nullable=False, default=Decimal("0"))
    avg_price: Mapped[Decimal] = mapped_column(MONEY, nullable=False, default=Decimal("0"))

    account: Mapped["Account"] = relationship(back_populates="positions")
    instrument: Mapped["Instrument"] = relationship(back_populates="positions")


class Trade(Base):
    """Immutable append-only event log. Never update or delete a row after insert."""

    __tablename__ = "trades"

    id: Mapped[int] = mapped_column(primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id"), nullable=False)
    instrument_id: Mapped[int] = mapped_column(ForeignKey("instruments.id"), nullable=False)
    order_id: Mapped[int | None] = mapped_column(ForeignKey("orders.id"), nullable=True)
    side: Mapped[TradeSide] = mapped_column(Enum(TradeSide), nullable=False)
    quantity: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    price: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    commission: Mapped[Decimal] = mapped_column(MONEY, nullable=False, default=Decimal("0"))
    executed_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, nullable=False)

    account: Mapped["Account"] = relationship(back_populates="trades")
    instrument: Mapped["Instrument"] = relationship(back_populates="trades")
    order: Mapped[Order | None] = relationship(back_populates="trades")


class Order(Base):
    """A request to trade. PENDING until a Broker fills or rejects it; the
    resulting Trade (if any) links back here via Trade.order_id. A Trade
    created by calling Portfolio.buy()/sell() directly (bypassing a broker)
    has order_id=NULL - that's expected, not an error.
    """

    __tablename__ = "orders"

    id: Mapped[int] = mapped_column(primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id"), nullable=False)
    instrument_id: Mapped[int] = mapped_column(ForeignKey("instruments.id"), nullable=False)
    side: Mapped[TradeSide] = mapped_column(Enum(TradeSide), nullable=False)
    quantity: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    order_type: Mapped[str] = mapped_column(String(16), nullable=False, default="market")
    status: Mapped[OrderStatus] = mapped_column(
        Enum(OrderStatus), nullable=False, default=OrderStatus.PENDING
    )
    commission: Mapped[Decimal] = mapped_column(MONEY, nullable=False, default=Decimal("0"))
    requested_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, nullable=False)
    filled_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    fill_price: Mapped[Decimal | None] = mapped_column(MONEY, nullable=True)
    broker_order_id: Mapped[str | None] = mapped_column(String(64), nullable=True)

    account: Mapped["Account"] = relationship(back_populates="orders")
    instrument: Mapped["Instrument"] = relationship(back_populates="orders")
    trades: Mapped[list["Trade"]] = relationship(back_populates="order")


class PriceBar(Base):
    """Locally cached OHLCV bar, so backtesting/UI refreshes don't re-hit the
    market data API for a range that's already been fetched."""

    __tablename__ = "price_bars"
    __table_args__ = (
        UniqueConstraint(
            "instrument_id", "timeframe", "timestamp", name="uq_price_bar_instrument_timeframe_ts"
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    instrument_id: Mapped[int] = mapped_column(ForeignKey("instruments.id"), nullable=False)
    timeframe: Mapped[str] = mapped_column(String(16), nullable=False)
    timestamp: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    open: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    high: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    low: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    close: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    volume: Mapped[int] = mapped_column(nullable=False)

    instrument: Mapped["Instrument"] = relationship(back_populates="price_bars")


class CachedDay(Base):
    """Marks a (instrument, timeframe, calendar day) as having been fully
    fetched from the upstream provider at least once - independent of how
    many PriceBar rows resulted, since a day with zero trading (e.g. a
    market holiday) is still "covered" once we've asked upstream for it.

    Used only for timeframes where a single bar does not necessarily equal
    a single calendar day's worth of data - everything except "Day" itself
    (Minute/Hour can have many bars per day; Week/Month bars each span more
    than a day). For "Day" timeframes, CachedMarketDataProvider still uses
    the simpler, original bar-presence check instead (see
    app/market/cache.py), since that check is exact at that one
    granularity and is what every bot in production relies on today.
    """

    __tablename__ = "cached_days"
    __table_args__ = (
        UniqueConstraint(
            "instrument_id", "timeframe", "day", name="uq_cached_day_instrument_timeframe_day"
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    instrument_id: Mapped[int] = mapped_column(ForeignKey("instruments.id"), nullable=False)
    timeframe: Mapped[str] = mapped_column(String(16), nullable=False)
    day: Mapped[date] = mapped_column(Date, nullable=False)

    instrument: Mapped["Instrument"] = relationship(back_populates="cached_days")


class Signal(Base):
    """Immutable log of every generate_signal() call, including WAIT - we
    want the full history of what a strategy said and when, not just the
    moments it happened to say BUY/SELL, so signal quality can later be
    analyzed independently of whether/when it was acted on.
    """

    __tablename__ = "signals"

    id: Mapped[int] = mapped_column(primary_key=True)
    strategy_name: Mapped[str] = mapped_column(String(64), nullable=False)
    strategy_params_json: Mapped[str] = mapped_column(Text, nullable=False)
    instrument_id: Mapped[int] = mapped_column(ForeignKey("instruments.id"), nullable=False)
    action: Mapped[SignalAction] = mapped_column(Enum(SignalAction), nullable=False)
    generated_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, nullable=False)
    price_at_signal: Mapped[Decimal | None] = mapped_column(MONEY, nullable=True)

    instrument: Mapped["Instrument"] = relationship(back_populates="signals")


class BacktestRun(Base):
    """One record per completed backtest run. Deliberately not linked to any
    Account - a backtest plays out on an isolated in-memory portfolio and
    must never touch (or even reference) the live one.
    """

    __tablename__ = "backtest_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    strategy_name: Mapped[str] = mapped_column(String(64), nullable=False)
    strategy_params_json: Mapped[str] = mapped_column(Text, nullable=False)
    instrument_id: Mapped[int] = mapped_column(ForeignKey("instruments.id"), nullable=False)
    start_date: Mapped[date] = mapped_column(Date, nullable=False)
    end_date: Mapped[date] = mapped_column(Date, nullable=False)
    initial_capital: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    final_capital: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    total_return_pct: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    max_drawdown_pct: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    win_rate_pct: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    num_trades: Mapped[int] = mapped_column(nullable=False)
    equity_curve_json: Mapped[str] = mapped_column(Text, nullable=False)
    risk_profile_name: Mapped[str | None] = mapped_column(String(32), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, nullable=False)

    instrument: Mapped["Instrument"] = relationship(back_populates="backtest_runs")


class BotConfig(Base):
    """A named pairing of (Strategy + params, a RiskProfile, a dedicated
    Account, a target symbol) that the automation daemon polls on its own
    cadence. Unlike Strategy/RiskProfile themselves (plain value objects,
    never persisted), a BotConfig genuinely needs to survive daemon
    restarts, so it's a real table - strategy_class/risk_profile_name are
    stored as plain strings and resolved back to the actual classes/value
    objects via the registries in app.strategies.registry / app.risk.risk_profile.
    """

    __tablename__ = "bot_configs"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    strategy_class: Mapped[str] = mapped_column(String(64), nullable=False)
    strategy_params_json: Mapped[str] = mapped_column(Text, nullable=False)
    risk_profile_name: Mapped[str] = mapped_column(String(32), nullable=False)
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id"), nullable=False)
    symbol: Mapped[str] = mapped_column(String(32), nullable=False)
    # Passed straight through to MarketDataProvider.get_bars() each poll
    # (see app.automation.daemon.run_bot_cycle) - defaults to "1Day" so
    # every bot created before this field existed keeps requesting exactly
    # what it always has. Added after bot_configs already had rows in
    # production, so app.database.session.get_engine() runs a one-off
    # ALTER TABLE to backfill this default onto existing rows - there's no
    # Alembic in this project, see get_engine()'s docstring.
    timeframe: Mapped[str] = mapped_column(String(16), nullable=False, default="1Day")
    # Which Broker app.automation.daemon.build_bot() constructs for this
    # bot: "paper" (default) -> PaperBroker, fully internal, never touches
    # a real account. "alpaca_paper" -> AlpacaBroker, which sends real
    # orders to Alpaca's *paper* trading endpoint (paper=True is hardcoded
    # inside AlpacaBroker itself - see its docstring - never read from here
    # or anywhere else, so this field can only ever choose *which* broker,
    # never whether it's paper). Defaults to "paper" so every bot created
    # before this field existed keeps its exact current behaviour. Added
    # after bot_configs already had rows in production, so
    # app.database.session.get_engine() migrates it the same way as
    # timeframe above.
    broker: Mapped[str] = mapped_column(String(16), nullable=False, default="paper")
    # Forces an open position closed at end-of-session (see
    # app.automation.daemon's force-session-close check), regardless of
    # what the strategy's signal says. Only meaningful for an intraday bot
    # that should never hold overnight - defaults to False so every
    # existing swing-style bot is completely unaffected.
    force_session_close: Mapped[bool] = mapped_column(nullable=False, default=False)
    poll_interval_seconds: Mapped[int] = mapped_column(nullable=False, default=300)
    enabled: Mapped[bool] = mapped_column(nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, nullable=False)

    # Persisted circuit-breaker state (Stage 8b): set when RiskManager trips
    # a bot's max_drawdown_pct halt, so the halt survives a daemon restart
    # instead of silently resetting. Cleared only by a manual action - see
    # app.automation.manage_bots.clear_halt().
    halted: Mapped[bool] = mapped_column(nullable=False, default=False)
    halted_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    halted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    account: Mapped["Account"] = relationship(back_populates="bot_configs")
