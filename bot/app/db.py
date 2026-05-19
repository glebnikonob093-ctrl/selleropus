from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

log = logging.getLogger(__name__)


def _unicode_lower(value: object) -> object:
    """SQLite-side ``LOWER`` that case-folds Unicode (including Cyrillic).

    SQLite's built-in ``LOWER`` only handles ASCII, so SQLAlchemy's ``ilike``
    fails to match "иван" against "Иван" on SQLite. Registering Python's
    Unicode-aware ``str.lower`` as the database's ``LOWER`` function fixes
    this transparently for every query.
    """
    if isinstance(value, str):
        return value.lower()
    return value


def ensure_sqlite_dir(database_url: str) -> None:
    """Make sure the parent directory of a SQLite database file exists."""
    if not database_url.startswith("sqlite"):
        return
    if "///" not in database_url:
        return
    path_str = database_url.split("///", 1)[1]
    path = Path(path_str).resolve() if path_str.startswith("./") else Path(path_str)
    path.parent.mkdir(parents=True, exist_ok=True)


def create_engine(database_url: str) -> AsyncEngine:
    engine = create_async_engine(database_url, future=True)
    if database_url.startswith("sqlite"):
        @event.listens_for(engine.sync_engine, "connect")
        def _register_unicode_lower(dbapi_conn, _connection_record):  # type: ignore[no-redef]
            # aiosqlite wraps sqlite3.Connection; fall back to the wrapper
            # itself for plain pysqlite engines.
            raw = getattr(dbapi_conn, "_conn", None) or dbapi_conn
            try:
                raw.create_function("lower", 1, _unicode_lower)
            except Exception:
                # If create_function isn't available we leave the default
                # ASCII-only LOWER in place — ilike will still work for
                # ASCII. Log loudly so the next time a user complains that
                # search "doesn't find Иван by иван" we have a breadcrumb.
                log.warning(
                    "sqlite create_function('lower') failed — "
                    "Cyrillic ILIKE will fall back to ASCII-only matching",
                    exc_info=True,
                )

    return engine


def create_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)


@asynccontextmanager
async def session_scope(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncSession]:
    session = session_factory()
    try:
        yield session
        await session.commit()
    except Exception:
        await session.rollback()
        raise
    finally:
        await session.close()


async def ping_db(engine: AsyncEngine) -> None:
    async with engine.begin() as conn:
        await conn.execute(text("SELECT 1"))
