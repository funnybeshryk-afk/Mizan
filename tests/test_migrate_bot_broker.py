from __future__ import annotations

import json
from decimal import Decimal

import pytest

from app.automation.migrate_bot_broker import (
    SWING_BOTS_MIGRATED_TO_ALPACA_PAPER,
    migrate_bot_broker,
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


def _make_legacy_bot_config(session, *, name, symbol, broker="paper", account_id=None):
    """Mimics exactly what a live VPS bot row looked like before Stage 14:
    broker="paper", a specific pre-existing account_id - not created via
    create_bot(), since that path already produces the new broker value."""
    if account_id is None:
        account = Account(cash=Decimal("10000"))
        session.add(account)
        session.commit()
        account_id = account.id

    bot_config = BotConfig(
        name=name,
        strategy_class="TrendStrategy",
        strategy_params_json=json.dumps({}),
        risk_profile_name="CONSERVATIVE",
        account_id=account_id,
        symbol=symbol,
        broker=broker,
        poll_interval_seconds=300,
        enabled=True,
    )
    session.add(bot_config)
    session.commit()
    return bot_config


def test_migrate_bot_broker_updates_broker_and_leaves_everything_else_untouched(session):
    bot_config = _make_legacy_bot_config(session, name="conservative-aapl", symbol="AAPL")
    original_account_id = bot_config.account_id

    updated = migrate_bot_broker(session, "conservative-aapl", "alpaca_paper")

    assert updated.broker == "alpaca_paper"
    assert updated.account_id == original_account_id
    assert updated.symbol == "AAPL"
    assert updated.risk_profile_name == "CONSERVATIVE"


def test_migrate_bot_broker_is_idempotent(session):
    _make_legacy_bot_config(session, name="conservative-aapl", symbol="AAPL")

    first = migrate_bot_broker(session, "conservative-aapl", "alpaca_paper")
    second = migrate_bot_broker(session, "conservative-aapl", "alpaca_paper")

    assert first.broker == "alpaca_paper"
    assert second.broker == "alpaca_paper"
    assert first.id == second.id


def test_migrate_bot_broker_no_ops_silently_when_already_on_target_broker(session, capsys):
    _make_legacy_bot_config(session, name="intraday-orb-spy", symbol="SPY", broker="alpaca_paper")

    migrate_bot_broker(session, "intraday-orb-spy", "alpaca_paper")

    captured = capsys.readouterr()
    assert "no-op" in captured.out
    assert "->" not in captured.out


def test_migrate_bot_broker_raises_for_an_unknown_bot_name(session):
    with pytest.raises(ValueError, match="no-such-bot"):
        migrate_bot_broker(session, "no-such-bot", "alpaca_paper")


def test_migrate_bot_broker_never_touches_a_bot_not_named(session):
    """Regression for the whole point of doing this per-name rather than a
    blanket UPDATE: migrating conservative-aapl must never affect
    intraday-orb-spy's already-correct broker or any other bot's row."""
    _make_legacy_bot_config(session, name="conservative-aapl", symbol="AAPL", broker="paper")
    orb = _make_legacy_bot_config(session, name="intraday-orb-spy", symbol="SPY", broker="alpaca_paper")
    orb_account_id = orb.account_id

    migrate_bot_broker(session, "conservative-aapl", "alpaca_paper")

    assert orb.broker == "alpaca_paper"
    assert orb.account_id == orb_account_id


def test_swing_bots_migrated_list_matches_the_four_live_swing_bots():
    """This is the exact list the live VPS migration run targets - keep it
    in sync with reality: 4 swing bots, never intraday-orb-spy (already
    alpaca_paper from Stage 13, must not be touched by this script)."""
    assert set(SWING_BOTS_MIGRATED_TO_ALPACA_PAPER) == {
        "conservative-aapl",
        "aggressive-tsla",
        "breakout-qqq",
        "meanrev-ko",
    }
    assert "intraday-orb-spy" not in SWING_BOTS_MIGRATED_TO_ALPACA_PAPER


def test_running_the_full_migration_list_against_all_five_live_bots(session):
    """End-to-end simulation of the real VPS run: all 5 bots exist exactly
    as they do in production (4x paper + intraday-orb-spy already
    alpaca_paper), migrate only the 4 named swing bots, and confirm the
    5th is left alone."""
    accounts_by_name = {}
    for name, symbol in [
        ("conservative-aapl", "AAPL"),
        ("aggressive-tsla", "TSLA"),
        ("breakout-qqq", "QQQ"),
        ("meanrev-ko", "KO"),
    ]:
        bot = _make_legacy_bot_config(session, name=name, symbol=symbol, broker="paper")
        accounts_by_name[name] = bot.account_id
    orb = _make_legacy_bot_config(session, name="intraday-orb-spy", symbol="SPY", broker="alpaca_paper")
    accounts_by_name["intraday-orb-spy"] = orb.account_id

    for name in SWING_BOTS_MIGRATED_TO_ALPACA_PAPER:
        migrate_bot_broker(session, name, "alpaca_paper")

    from app.automation.setup_bots import get_bot_config_by_name

    for name in SWING_BOTS_MIGRATED_TO_ALPACA_PAPER:
        bot_config = get_bot_config_by_name(session, name)
        assert bot_config.broker == "alpaca_paper"
        assert bot_config.account_id == accounts_by_name[name]

    orb_config = get_bot_config_by_name(session, "intraday-orb-spy")
    assert orb_config.broker == "alpaca_paper"
    assert orb_config.account_id == accounts_by_name["intraday-orb-spy"]
