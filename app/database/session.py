from __future__ import annotations

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import sessionmaker

from app.core.paths import DATA_DIR, DB_PATH
from app.database.models import Base


def get_engine(db_url: str | None = None) -> Engine:
    """Create the SQLAlchemy engine and ensure all tables exist.

    Pass db_url="sqlite:///:memory:" for tests; defaults to the on-disk
    database at data/mizan.db - next to the .exe when running as a frozen
    build, in the repo root in dev mode (see app.core.paths).
    """
    if db_url is None:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        db_url = f"sqlite:///{DB_PATH}"
    engine = create_engine(db_url, future=True)
    Base.metadata.create_all(engine)
    _ensure_bot_config_columns(engine)
    return engine


# Columns added to BotConfig after bot_configs already had real rows (the
# live daemon's 4 bots) - each entry's default reproduces exactly what
# every existing bot has always implicitly meant, so migrating never
# changes their observed behaviour.
_BOT_CONFIG_MIGRATED_COLUMNS: list[tuple[str, str]] = [
    ("timeframe", "VARCHAR(16) NOT NULL DEFAULT '1Day'"),
    ("broker", "VARCHAR(16) NOT NULL DEFAULT 'paper'"),
    ("force_session_close", "BOOLEAN NOT NULL DEFAULT 0"),
]


def _ensure_bot_config_columns(engine: Engine) -> None:
    """create_all() above only creates missing TABLES - it never alters
    one that already exists on disk. There's no Alembic in this project,
    so each column added to BotConfig after it already had rows in
    production needs one explicit, idempotent ALTER TABLE here instead.
    Safe to call on every startup: a no-op once a column exists (including
    for a fresh in-memory test DB, where create_all() already created it
    with every column present).
    """
    with engine.begin() as conn:
        existing_columns = {row[1] for row in conn.execute(text("PRAGMA table_info(bot_configs)"))}
        for column_name, ddl in _BOT_CONFIG_MIGRATED_COLUMNS:
            if column_name not in existing_columns:
                conn.execute(text(f"ALTER TABLE bot_configs ADD COLUMN {column_name} {ddl}"))


def get_session_factory(engine: Engine | None = None) -> sessionmaker:
    engine = engine or get_engine()
    return sessionmaker(bind=engine, expire_on_commit=False, future=True)
