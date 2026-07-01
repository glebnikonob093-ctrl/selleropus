from __future__ import annotations

import logging

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from app.models import Base

log = logging.getLogger(__name__)


async def create_all(engine: AsyncEngine) -> None:
    """Create all tables that do not exist yet.

    For the MVP we intentionally do not use Alembic. Adding a new column
    here is fine; renaming/dropping requires a hand-written ALTER below.
    """
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    await _add_is_master_column(engine)


async def _add_is_master_column(engine: AsyncEngine) -> None:
    """Idempotent ALTER: add ``is_master`` column to masters, backfill existing rows."""
    async with engine.begin() as conn:
        try:
            await conn.execute(
                text("ALTER TABLE masters ADD COLUMN is_master BOOLEAN DEFAULT 0")
            )
            log.info("Added is_master column to masters table")
        except Exception:
            pass  # column already exists
        # Backfill: all pre-existing rows are real masters.
        await conn.execute(text("UPDATE masters SET is_master = 1 WHERE is_master IS NULL OR is_master = 0"))
