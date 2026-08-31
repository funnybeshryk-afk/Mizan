from __future__ import annotations

import json
from dataclasses import dataclass, field
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database.models import BotConfig
from app.portfolio.portfolio import Portfolio

DEFAULT_BOT_INITIAL_CASH = Decimal("10000")


@dataclass(frozen=True)
class BotSpec:
    """A one-time blueprint for creating a BotConfig + its dedicated
    Account. Not persisted itself - just the input to create_bot()."""

    name: str
    strategy_class: str
    risk_profile_name: str
    symbol: str
    timeframe: str = "1Day"
    broker: str = "paper"
    force_session_close: bool = False
    poll_interval_seconds: int = 300
    initial_cash: Decimal = DEFAULT_BOT_INITIAL_CASH
    strategy_params: dict = field(default_factory=dict)


# The original two bots (TrendStrategy: one CONSERVATIVE, one AGGRESSIVE)
# plus two paper-observation bots added in Stage 10 for the newer
# strategies (BreakoutStrategy, MeanReversionStrategy) - each with its own
# dedicated account, same as every bot here. Two CONSERVATIVE bots
# (conservative-aapl and breakout-qqq/meanrev-ko) do NOT share or split a
# capital pool - RiskProfile.capital_allocation_pct is not enforced
# anywhere (see its docstring in app/risk/risk_profile.py), so each gets
# its own full, independent paper account regardless of how many other
# bots use the same risk_profile_name.
#
# KNOWN LIMITATION (Stage 14): all 5 bots below now use broker=
# "alpaca_paper", meaning all 5 submit real orders to the *same* shared
# Alpaca paper account - yet each still has its own independent local
# Account ledger here (DEFAULT_BOT_INITIAL_CASH each), inherited unchanged
# from when only PaperBroker bots existed and every bot's local ledger WAS
# the only source of truth. The local ledgers and Alpaca's real balance are
# now two independent, divergent sources of truth for "how much capital
# does this bot have" - summing the 5 local accounts will not equal
# Alpaca's real balance, and nothing here reconciles that. This is
# deliberate, not a bug: proper shared-capital accounting belongs to
# RiskProfile.capital_allocation_pct enforcement (see its docstring), part
# of the Paper -> Live promotion criteria, not this stage. See
# app.automation.daemon._check_alpaca_capital_consistency, which logs a
# warning at every daemon startup so this divergence is loud, not silent.
DEFAULT_BOT_SPECS: list[BotSpec] = [
    BotSpec(
        name="conservative-aapl",
        strategy_class="TrendStrategy",
        risk_profile_name="CONSERVATIVE",
        symbol="AAPL",
        broker="alpaca_paper",
    ),
    BotSpec(
        name="aggressive-tsla",
        strategy_class="TrendStrategy",
        risk_profile_name="AGGRESSIVE",
        symbol="TSLA",
        broker="alpaca_paper",
    ),
    BotSpec(
        name="breakout-qqq",
        strategy_class="BreakoutStrategy",
        risk_profile_name="CONSERVATIVE",
        symbol="QQQ",
        broker="alpaca_paper",
    ),
    BotSpec(
        name="meanrev-ko",
        strategy_class="MeanReversionStrategy",
        risk_profile_name="CONSERVATIVE",
        symbol="KO",
        broker="alpaca_paper",
    ),
    # First intraday bot (Stage 13): real Alpaca paper execution (not the
    # internal PaperBroker every other bot above uses), 1-minute bars, a
    # fast poll cadence, and force_session_close=True since holding an ORB
    # position overnight would defeat the point of it being intraday.
    BotSpec(
        name="intraday-orb-spy",
        strategy_class="OpeningRangeBreakoutStrategy",
        risk_profile_name="CONSERVATIVE",
        symbol="SPY",
        timeframe="1Min",
        broker="alpaca_paper",
        force_session_close=True,
        poll_interval_seconds=60,
        strategy_params={"opening_range_minutes": 15},
    ),
]


def get_bot_config_by_name(session: Session, name: str) -> BotConfig | None:
    return session.execute(select(BotConfig).where(BotConfig.name == name)).scalar_one_or_none()


def create_bot(session: Session, spec: BotSpec) -> BotConfig:
    """Creates one bot: a fresh, dedicated Account - via Portfolio.create(),
    the same account-creation path everything else in the app uses, never
    a bespoke Account() construction - plus the BotConfig row pointing at
    it. Idempotent: if a bot with this name already exists, it's returned
    unchanged instead of creating a duplicate account.
    """
    existing = get_bot_config_by_name(session, spec.name)
    if existing is not None:
        return existing

    portfolio = Portfolio.create(session, initial_cash=spec.initial_cash)

    bot_config = BotConfig(
        name=spec.name,
        strategy_class=spec.strategy_class,
        strategy_params_json=json.dumps(spec.strategy_params, sort_keys=True),
        risk_profile_name=spec.risk_profile_name,
        account_id=portfolio.account.id,
        symbol=spec.symbol,
        timeframe=spec.timeframe,
        broker=spec.broker,
        force_session_close=spec.force_session_close,
        poll_interval_seconds=spec.poll_interval_seconds,
        enabled=True,
    )
    session.add(bot_config)
    session.commit()
    return bot_config


def create_default_bots(session: Session) -> list[BotConfig]:
    """All bots in DEFAULT_BOT_SPECS, each with its own dedicated account -
    safe to call more than once, since create_bot() is idempotent per bot
    name."""
    return [create_bot(session, spec) for spec in DEFAULT_BOT_SPECS]


if __name__ == "__main__":
    from app.database.session import get_engine, get_session_factory

    db_session = get_session_factory(get_engine())()
    try:
        for bot in create_default_bots(db_session):
            print(
                f"{bot.name}: account_id={bot.account_id} symbol={bot.symbol} "
                f"risk_profile={bot.risk_profile_name} poll_interval_seconds={bot.poll_interval_seconds}"
            )
    finally:
        db_session.close()
