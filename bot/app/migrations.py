from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncEngine

from app.models import Base


async def create_all(engine: AsyncEngine) -> None:
    """Create all tables that do not exist yet.

    For the MVP we intentionally do not use Alembic. Adding a new column
    here is fine; renaming/dropping requires a hand-written ALTER below.
    """
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
