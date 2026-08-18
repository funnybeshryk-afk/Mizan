from __future__ import annotations

import sys

from sqlalchemy.orm import Session

from app.automation.setup_bots import get_bot_config_by_name
from app.database.models import BotConfig


def clear_halt(session: Session, name: str) -> BotConfig:
    """Manually resets a bot's persisted circuit-breaker halt (see
    app.automation.daemon._execute_signal, where RiskManager tripping
    max_drawdown_pct sets BotConfig.halted=True). There is no automatic
    recovery by design - a drawdown circuit breaker is meant to force a
    human to look before the bot trades again.
    """
    bot_config = get_bot_config_by_name(session, name)
    if bot_config is None:
        raise ValueError(f"No bot named {name!r}")

    bot_config.halted = False
    bot_config.halted_reason = None
    bot_config.halted_at = None
    session.commit()
    return bot_config


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("Usage: python -m app.automation.manage_bots <bot-name>")
        raise SystemExit(1)

    from app.database.session import get_engine, get_session_factory

    bot_name = sys.argv[1]
    db_session = get_session_factory(get_engine())()
    try:
        cleared = clear_halt(db_session, bot_name)
        print(f"Cleared halt for {cleared.name!r} - it will resume on the next daemon start.")
    finally:
        db_session.close()
