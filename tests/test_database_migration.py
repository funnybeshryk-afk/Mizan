from __future__ import annotations

import sqlite3

from sqlalchemy import select

from app.database.models import BotConfig
from app.database.session import get_engine, get_session_factory


def test_get_engine_adds_the_new_bot_config_columns_to_a_pre_existing_table(tmp_path):
    """Base.metadata.create_all() only creates missing TABLES - it never
    alters one that already exists on disk. This reproduces exactly the
    live daemon's real situation (a bot_configs table created, and
    populated with real bots, before BotConfig.timeframe/broker/
    force_session_close existed) and confirms get_engine() migrates it in
    place on the next startup: no crash, and the pre-existing row ends up
    with timeframe="1Day", broker="paper", force_session_close=False -
    without any of its other data being disturbed.
    """
    db_path = tmp_path / "legacy.db"
    db_url = f"sqlite:///{db_path}"

    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        CREATE TABLE accounts (
            id INTEGER PRIMARY KEY,
            cash NUMERIC(18, 6) NOT NULL,
            created_at DATETIME NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE bot_configs (
            id INTEGER PRIMARY KEY,
            name VARCHAR(64) NOT NULL UNIQUE,
            strategy_class VARCHAR(64) NOT NULL,
            strategy_params_json TEXT NOT NULL,
            risk_profile_name VARCHAR(32) NOT NULL,
            account_id INTEGER NOT NULL,
            symbol VARCHAR(32) NOT NULL,
            poll_interval_seconds INTEGER NOT NULL,
            enabled BOOLEAN NOT NULL,
            created_at DATETIME NOT NULL,
            halted BOOLEAN NOT NULL,
            halted_reason TEXT,
            halted_at DATETIME
        )
        """
    )
    conn.execute("INSERT INTO accounts (id, cash, created_at) VALUES (1, 10000, '2024-01-01 00:00:00')")
    conn.execute(
        """
        INSERT INTO bot_configs
        (id, name, strategy_class, strategy_params_json, risk_profile_name, account_id, symbol,
         poll_interval_seconds, enabled, created_at, halted, halted_reason, halted_at)
        VALUES (1, 'conservative-aapl', 'TrendStrategy', '{}', 'CONSERVATIVE', 1, 'AAPL',
                300, 1, '2024-01-01 00:00:00', 0, NULL, NULL)
        """
    )
    conn.commit()
    conn.close()

    # This is exactly what daemon startup does against a real, already-
    # populated database - must not raise, and must leave the pre-existing
    # bot's own data untouched apart from the new column's default.
    engine = get_engine(db_url)
    session = get_session_factory(engine)()
    try:
        bot_config = session.execute(
            select(BotConfig).where(BotConfig.name == "conservative-aapl")
        ).scalar_one()
        assert bot_config.timeframe == "1Day"
        assert bot_config.broker == "paper"
        assert bot_config.force_session_close is False
        assert bot_config.symbol == "AAPL"
        assert bot_config.poll_interval_seconds == 300
        assert bot_config.account_id == 1
    finally:
        session.close()

    # Idempotent: calling get_engine() again against the now-migrated file
    # must not try to add the column a second time.
    get_engine(db_url)
