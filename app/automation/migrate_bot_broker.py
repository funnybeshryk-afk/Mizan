from __future__ import annotations

from sqlalchemy.orm import Session

from app.automation.setup_bots import get_bot_config_by_name
from app.database.models import BotConfig

# Stage 14: DEFAULT_BOT_SPECS (app.automation.setup_bots) now describes all
# 5 default bots with broker="alpaca_paper" - but create_bot() is
# deliberately idempotent by name (see its docstring): it never updates an
# existing row, only returns it unchanged. Any bot already created before
# this change (the 4 swing bots on the live VPS, seeded back when
# broker="paper" was the only value that existed) keeps its old broker
# forever unless something explicitly updates it - deploying the new
# DEFAULT_BOT_SPECS and restarting the daemon does NOT do this on its own.
#
# This is deliberately its own one-off script, not folded into
# app.database.session._ensure_bot_config_columns (the project's existing
# migration mechanism): that function's job is a structural guarantee -
# "this column exists, with this default" - safe to run unconditionally on
# every startup forever, for any database old or new, because a column
# either exists or doesn't. Updating broker for 4 specific *named* rows is
# a one-time data correction, not a structural guarantee about every
# database; wiring it into permanent startup logic would make it
# permanently, silently re-assert broker="alpaca_paper" for these names on
# every future daemon start, fighting any deliberate future change an
# operator makes to one bot's broker - the opposite of create_bot()'s own
# "never touch an existing row" philosophy. Run once, by hand, per
# deployment that needs it.
SWING_BOTS_MIGRATED_TO_ALPACA_PAPER: list[str] = [
    "conservative-aapl",
    "aggressive-tsla",
    "breakout-qqq",
    "meanrev-ko",
]


def migrate_bot_broker(session: Session, name: str, broker: str) -> BotConfig:
    """Updates one bot's broker in place - idempotent (a no-op, not an
    error, if it's already on the target broker) but NOT idempotent-by-
    creation the way create_bot() is: a missing bot name is a real error
    here (something is wrong with the deploy - the row this script expects
    to update doesn't exist), not a "create it fresh" case.

    Touches only the broker column - account_id, initial cash, and every
    other field on the row are left exactly as they were.
    """
    bot_config = get_bot_config_by_name(session, name)
    if bot_config is None:
        raise ValueError(f"No bot named {name!r} - nothing to migrate")

    if bot_config.broker != broker:
        old_broker = bot_config.broker
        bot_config.broker = broker
        session.commit()
        print(f"{name}: broker {old_broker!r} -> {broker!r} (account_id={bot_config.account_id} unchanged)")
    else:
        print(f"{name}: already broker={broker!r} - no-op")

    return bot_config


if __name__ == "__main__":
    from app.database.session import get_engine, get_session_factory

    db_session = get_session_factory(get_engine())()
    try:
        for bot_name in SWING_BOTS_MIGRATED_TO_ALPACA_PAPER:
            migrate_bot_broker(db_session, bot_name, "alpaca_paper")
    finally:
        db_session.close()
