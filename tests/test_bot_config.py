from __future__ import annotations

import json
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.automation.setup_bots import (
    DEFAULT_BOT_SPECS,
    BotSpec,
    create_bot,
    create_default_bots,
    get_bot_config_by_name,
)
from app.database.models import Account, BotConfig
from app.database.session import get_engine, get_session_factory


@pytest.fixture
def session():
    engine = get_engine("sqlite:///:memory:")
    session_factory = get_session_factory(engine)
    session = session_factory()
    yield session
    session.close()


def test_bot_config_round_trips_through_the_database(session):
    account = Account(cash=Decimal("5000"))
    session.add(account)
    session.commit()

    bot_config = BotConfig(
        name="conservative-aapl",
        strategy_class="TrendStrategy",
        strategy_params_json=json.dumps({"ema_fast_period": 12}),
        risk_profile_name="CONSERVATIVE",
        account_id=account.id,
        symbol="AAPL",
        poll_interval_seconds=60,
        enabled=True,
    )
    session.add(bot_config)
    session.commit()

    loaded = session.execute(select(BotConfig).where(BotConfig.name == "conservative-aapl")).scalar_one()
    assert loaded.strategy_class == "TrendStrategy"
    assert json.loads(loaded.strategy_params_json) == {"ema_fast_period": 12}
    assert loaded.risk_profile_name == "CONSERVATIVE"
    assert loaded.account_id == account.id
    assert loaded.symbol == "AAPL"
    assert loaded.poll_interval_seconds == 60
    assert loaded.enabled is True
    assert loaded.created_at is not None
    assert loaded.account is account


def test_create_bot_gives_each_bot_its_own_dedicated_account(session):
    spec = BotSpec(
        name="conservative-aapl",
        strategy_class="TrendStrategy",
        risk_profile_name="CONSERVATIVE",
        symbol="AAPL",
        initial_cash=Decimal("7500"),
    )
    bot_config = create_bot(session, spec)

    account = session.get(Account, bot_config.account_id)
    assert account is not None
    assert account.cash == Decimal("7500")


def test_create_bot_is_idempotent_by_name(session):
    spec = BotSpec(
        name="conservative-aapl",
        strategy_class="TrendStrategy",
        risk_profile_name="CONSERVATIVE",
        symbol="AAPL",
    )
    first = create_bot(session, spec)
    second = create_bot(session, spec)

    assert first.id == second.id
    all_bots = list(session.execute(select(BotConfig)).scalars())
    assert len(all_bots) == 1


def test_create_default_bots_creates_all_bots_with_separate_accounts(session):
    bots = create_default_bots(session)

    assert len(bots) == len(DEFAULT_BOT_SPECS)
    names = {bot.name for bot in bots}
    assert names == {
        "conservative-aapl",
        "aggressive-tsla",
        "breakout-qqq",
        "meanrev-ko",
        "intraday-orb-spy",
    }

    account_ids = {bot.account_id for bot in bots}
    assert len(account_ids) == len(bots)  # every bot has its own account, capital is never shared

    conservative = get_bot_config_by_name(session, "conservative-aapl")
    aggressive = get_bot_config_by_name(session, "aggressive-tsla")
    breakout = get_bot_config_by_name(session, "breakout-qqq")
    mean_reversion = get_bot_config_by_name(session, "meanrev-ko")
    orb = get_bot_config_by_name(session, "intraday-orb-spy")
    # Stage 14: all 5 bots (these 4 plus intraday-orb-spy below) now trade
    # through real Alpaca paper execution - broker="paper"/PaperBroker is
    # still a supported value (see test_default_broker_bot_still_constructs_
    # a_paperbroker in test_daemon.py), just no longer what any default bot
    # uses.
    assert conservative.risk_profile_name == "CONSERVATIVE"
    assert conservative.symbol == "AAPL"
    assert conservative.broker == "alpaca_paper"
    assert conservative.force_session_close is False
    assert aggressive.risk_profile_name == "AGGRESSIVE"
    assert aggressive.symbol == "TSLA"
    assert aggressive.broker == "alpaca_paper"
    assert aggressive.force_session_close is False
    assert breakout.strategy_class == "BreakoutStrategy"
    assert breakout.risk_profile_name == "CONSERVATIVE"
    assert breakout.symbol == "QQQ"
    assert breakout.broker == "alpaca_paper"
    assert breakout.force_session_close is False
    assert mean_reversion.strategy_class == "MeanReversionStrategy"
    assert mean_reversion.risk_profile_name == "CONSERVATIVE"
    assert mean_reversion.symbol == "KO"
    assert mean_reversion.broker == "alpaca_paper"
    assert mean_reversion.force_session_close is False

    # The new intraday bot: real Alpaca paper execution, 1-minute bars,
    # forced end-of-session close - none of which any existing bot has.
    assert orb.strategy_class == "OpeningRangeBreakoutStrategy"
    assert orb.symbol == "SPY"
    assert orb.timeframe == "1Min"
    assert orb.broker == "alpaca_paper"
    assert orb.force_session_close is True
    assert orb.poll_interval_seconds == 60

    # Two bots (conservative-aapl, breakout-qqq, meanrev-ko) share
    # risk_profile_name="CONSERVATIVE" - confirm they still got fully
    # independent accounts, not some shared/split allocation.
    conservative_profile_bots = [conservative, breakout, mean_reversion]
    assert len({b.account_id for b in conservative_profile_bots}) == 3


def test_create_default_bots_is_safe_to_call_twice(session):
    create_default_bots(session)
    create_default_bots(session)

    all_bots = list(session.execute(select(BotConfig)).scalars())
    assert len(all_bots) == len(DEFAULT_BOT_SPECS)
